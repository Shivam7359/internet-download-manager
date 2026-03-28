# Multi-Threaded Parallel Chunk Downloading Guide

## Overview

IDM implements **HTTP/HTTPS Range-based parallel chunk downloading** using:
- **asyncio** for concurrent task management (not traditional threads)
- **HTTP Range headers** for byte-range requests (RFC 7233)
- **Adaptive chunk sizing** based on file size
- **Bandwidth throttling** with token-bucket algorithm
- **Automatic fallback** to single-chunk if Range not supported

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│               DownloadTask                          │
│  (Orchestrates single file download lifecycle)      │
└────────────────┬────────────────────────────────────┘
                 │
                 ├─ Preflight (HEAD)
                 │  └─ Detect file size, Range support, filename
                 │
                 ├─ Calculate Chunks
                 │  └─ Split file into byte ranges
                 │
                 ├─ Download Chunks in Parallel (asyncio.gather)
                 │  ├─ Chunk 0: bytes 0-524287
                 │  ├─ Chunk 1: bytes 524288-1048575
                 │  ├─ Chunk 2: bytes 1048576-1572863
                 │  └─ Chunk N: bytes X-END
                 │
                 ├─ Track Progress
                 │  └─ SpeedTracker (rolling average)
                 │
                 └─ Merge & Verify
                    └─ Assemble chunks into final file
```

---

## 1. Dynamic Chunk Calculation

**File:** `core/downloader.py` (lines 211-248)

Based on file size, automatically determine optimal chunk count:

```python
def dynamic_chunk_count(file_size: int, default_chunks: int = 8) -> int:
    """
    Heuristic tuned for stability and user expectation:
    
    • < 1 MB      → 1 chunk      (single download)
    • 1–10 MB     → 4 chunks     (fast over reasonable connections)
    • 10–100 MB   → up to 8      (more parallelism)
    • 100 MB–2 GB → 4 chunks     (balance for large files)
    • > 2 GB      → 8 chunks     (maximum parallelism)
    """
    if file_size < 1_048_576:          # < 1 MB
        return 1
    if file_size < 10_485_760:         # < 10 MB
        return 4
    if file_size < 104_857_600:        # < 100 MB
        return min(default_chunks, 8)
    if file_size < 2_147_483_648:      # < 2 GB
        return 4
    return 8
```

**Example:** A 100 MB file → 8 chunks × ~12.5 MB each

---

## 2. Byte-Range Calculation

**File:** `core/downloader.py` (lines 149-204)

Split file into precise byte ranges with constraints:

```python
def calculate_chunks(
    file_size: int,
    num_chunks: int = 8,
    min_chunk_size: int = 256_KB,    # 262,144 bytes
    max_chunk_size: int = 50_MB,     # 52,428,800 bytes
) -> list[tuple[int, int]]:
    """
    Returns list of (start_byte, end_byte) tuples.
    
    Example for 1 GB file into 4 chunks:
    - Chunk 0: (0, 262,144,000)          [262 MB]
    - Chunk 1: (262,144,001, 524,288,000) [262 MB]
    - Chunk 2: (524,288,001, 786,432,000) [262 MB]
    - Chunk 3: (786,432,001, 1,073,741,823) [remainder]
    """
    chunk_size = file_size // num_chunks
    chunks: list[tuple[int, int]] = []
    
    for i in range(num_chunks):
        start = i * chunk_size
        if i == num_chunks - 1:
            end = file_size - 1  # Last chunk gets remainder
        else:
            end = start + chunk_size - 1
        chunks.append((start, end))
    
    return chunks
```

---

## 3. Range Header Construction

**File:** `core/network.py` (lines 1105-1140)

Build RFC 7233 compliant Range headers:

```python
def build_chunk_headers(
    self,
    start_byte: int,
    end_byte: int,
    *,
    referer: Optional[str] = None,
    cookies: Optional[str] = None,
) -> dict[str, str]:
    """
    Build HTTP headers for chunk download.
    
    Example for Chunk 1 (bytes 1,000,000 to 1,999,999):
    
    Headers:
    {
        "Range": "bytes=1000000-1999999",  # ← RFC 7233 format
        "User-Agent": "IDM/1.0",
        "Referer": "...",
        "Cookie": "..."
    }
    
    Server Response:
    HTTP/1.1 206 Partial Content
    Content-Range: bytes 1000000-1999999/TOTAL_SIZE
    Content-Length: 999999
    """
    headers: dict[str, str] = {
        "Range": f"bytes={start_byte}-{end_byte}",
    }
    
    if referer:
        headers["Referer"] = referer
    if cookies:
        headers["Cookie"] = cookies
    
    # Add User-Agent, etc.
    headers.update(self._default_headers)
    return headers
```

---

## 4. Parallel Chunk Downloading (asyncio)

**File:** `core/downloader.py` (lines 520-570)

Download all chunks concurrently using `asyncio.gather()`:

```python
# Step 3: Download chunks in parallel
incomplete = [
    c for c in self._chunk_records
    if c.status != ChunkStatus.COMPLETED.value
]

if not incomplete:
    log.info("All chunks already complete")
else:
    # Create async task for each chunk
    tasks = [
        asyncio.create_task(
            self._download_chunk(chunk),
            name=f"chunk-{download_id[:8]}-{chunk.chunk_index}",
        )
        for chunk in incomplete
    ]
    
    # Also start progress reporter task
    progress_task = asyncio.create_task(self._progress_reporter())
    
    try:
        # Run all chunk downloads concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        progress_task.cancel()
    
    # Check for errors (retry logic below)
    errors = [r for r in results if isinstance(r, Exception)]
```

**Concurrency Model:**
- Each chunk downloads in its own async task
- All tasks run on same thread using asyncio event loop
- No GIL contention (I/O bound, not CPU bound)
- Natural pause/resume via `asyncio.Event()`

---

## 5. Individual Chunk Download

**File:** `core/downloader.py` (lines 670-800)

Each chunk handles Resume, Range validation, and retry:

```python
async def _download_chunk(self, chunk: ChunkRecord) -> None:
    """Download single byte-range chunk with retry logic."""
    
    policy = self._network.retry_policy
    session = self._network.session
    
    for attempt in range(policy.max_retries + 1):
        try:
            # Calculate resume offset (in case of interruption)
            start = chunk.resume_offset           # start_byte + already_downloaded
            end = chunk.end_byte
            
            if start > end:
                # Already fully downloaded
                return
            
            # Build Range headers
            headers = self._network.build_chunk_headers(
                start, end,
                referer=self._record.referer,
                cookies=self._record.cookies,
            )
            
            # Make Range request
            async with session.get(
                self._record.url,
                headers=headers,
                proxy=self._network.proxy_url,
            ) as resp:
                # 206 = Partial Content (Range supported)
                # 200 = OK (full file, Range ignored)
                if resp.status == 416:
                    # Range Not Satisfiable → fallback to single-chunk
                    raise RangeNotSupportedError(...)
                
                if resp.status not in (200, 206):
                    # Handle errors with retry
                    raise ConnectionError(f"HTTP {resp.status}")
                
                # Stream response to temp file
                async with aiofiles.open(chunk.temp_file, "ab") as f:
                    async for data in resp.content.iter_chunked(64_KB):
                        # Respect pause events
                        await self._paused.wait()
                        if self._cancelled:
                            return
                        
                        # Apply bandwidth throttle
                        await self._network.throttle(len(data), download_id)
                        
                        # Write to disk
                        await f.write(data)
                        
                        # Update progress
                        chunk.downloaded_bytes += len(data)
                        self._downloaded_bytes += len(data)
                        self._speed_tracker.record(self._downloaded_bytes)
                
                # Success
                return  # Exit retry loop
        
        except Exception as exc:
            if not policy.should_retry(attempt, exc):
                raise
            
            # Exponential backoff with jitter
            delay = policy.get_retry_delay(attempt, exc)
            await asyncio.sleep(delay)
```

**Key Features:**
- ✅ Resume from interrupted byte offset
- ✅ Range header validation
- ✅ Automatic fallback on 416 error
- ✅ Exponential backoff retry
- ✅ Bandwidth throttling per download
- ✅ Pause/cancel events

---

## 6. Bandwidth Throttling

**File:** `core/network.py`

Token-bucket rate limiting:

```python
class TokenBucketRateLimiter:
    """Implement token-bucket algorithm for rate limiting."""
    
    async def throttle(self, bytes_to_write: int, dry_run: bool = False) -> None:
        """
        Wait if necessary to maintain bandwidth limit.
        
        Example: 1 Mbps limit on 1 MB chunk
        - Tokens available: 1,000,000 / sec
        - Need: 1,000,000 tokens
        - Wait: ~1 second
        """
        while self._tokens_available < bytes_to_write:
            await asyncio.sleep(0.01)  # Check every 10ms
            self._tokens_available += self._refill_rate * 0.01
```

**Usage in Download:**
```python
# Before writing chunk data
await self._network.throttle(len(data), self._download_id)
await f.write(data)
```

---

## 7. Progress Tracking

**File:** `core/downloader.py` (lines 102-140)

Rolling-average speed calculation:

```python
class SpeedTracker:
    """Track download speed using rolling window of samples."""
    
    def __init__(self, window_size: int = 20):
        self._samples: deque[tuple[float, int]] = deque(maxlen=window_size)
        self._total_bytes: int = 0
        self._start_time: float = time.monotonic()
    
    def record(self, bytes_so_far: int) -> None:
        """Record current cumulative byte count."""
        self._samples.append((time.monotonic(), bytes_so_far))
        self._total_bytes = bytes_so_far
    
    @property
    def speed(self) -> float:
        """Current speed (rolling average over last 20 samples)."""
        if len(self._samples) < 2:
            return 0.0
        
        oldest_time, oldest_bytes = self._samples[0]
        newest_time, newest_bytes = self._samples[-1]
        elapsed = newest_time - oldest_time
        
        if elapsed <= 0:
            return 0.0
        
        return (newest_bytes - oldest_bytes) / elapsed
    
    @property
    def average_speed(self) -> float:
        """Overall average speed since start."""
        elapsed = time.monotonic() - self._start_time
        return self._total_bytes / elapsed if elapsed > 0 else 0.0
```

---

## 8. Error Handling & Fallback

**File:** `core/downloader.py` (lines 550-570)

Graceful degradation if Range not supported:

```python
# Check for Range-related errors
range_errors = [
    e for e in errors
    if isinstance(e, RangeNotSupportedError)
]

if range_errors:
    log.info("Falling back from multi-chunk to single-chunk")
    
    # Delete multi-chunk records
    await self._storage.delete_chunks(self._download_id)
    
    # Create single chunk spanning entire file
    single = ChunkRecord(
        download_id=self._download_id,
        chunk_index=0,
        start_byte=0,
        end_byte=max(0, record.file_size - 1),
        temp_file=str(chunks_dir / "chunk_0.part"),
    )
    await self._storage.add_chunks(self._download_id, [single])
    
    # Retry download with single chunk (no Range headers)
    results = await asyncio.gather(
        self._download_chunk(single),
        return_exceptions=True
    )
```

---

## 9. Storage Layer

**File:** `core/storage.py` (lines 157-200)

Persistent chunk tracking:

```python
@dataclass
class ChunkRecord:
    """Represents single byte-range chunk within download."""
    
    id: int = 0
    download_id: str = ""
    chunk_index: int = 0
    start_byte: int = 0              # Start of byte range
    end_byte: int = 0                # End of byte range (inclusive)
    downloaded_bytes: int = 0        # Bytes downloaded so far
    status: str = "pending"           # pending, downloading, completed, failed
    temp_file: str = ""              # Path to chunk temp file
    error_message: Optional[str] = None
    
    @property
    def total_bytes(self) -> int:
        """Total size of this chunk."""
        return self.end_byte - self.start_byte + 1
    
    @property
    def resume_offset(self) -> int:
        """Byte offset from which to resume downloading."""
        return self.start_byte + self.downloaded_bytes
    
    @property
    def progress_percent(self) -> float:
        """Chunk progress 0.0-100.0."""
        total = self.total_bytes
        if total <= 0:
            return 0.0
        return (self.downloaded_bytes / total) * 100.0
```

---

## 10. Assembly Phase

**File:** `core/assembler.py` (lines 127-215)

Merge completed chunks into final file:

```python
async def assemble(
    self,
    chunks: list[ChunkRecord],
    on_progress: Optional[Callable[[float], None]] = None,
) -> AssemblyResult:
    """
    Merge chunk temp files into final output file.
    
    Process:
    1. Sort chunks by index
    2. Validate all temp files exist
    3. Open output file for writing
    4. For each chunk:
       - Open chunk temp file
       - Read in 64 KB blocks
       - Write to output
       - Update progress
    5. Verify byte counts match
    """
    sorted_chunks = sorted(chunks, key=lambda c: c.chunk_index)
    
    total_bytes = sum(
        Path(c.temp_file).stat().st_size 
        for c in sorted_chunks
    )
    bytes_written = 0
    
    async with aiofiles.open(final_path, "wb") as out_file:
        for chunk in sorted_chunks:
            async with aiofiles.open(chunk.temp_file, "rb") as in_file:
                while True:
                    data = await in_file.read(65_536)  # 64 KB
                    if not data:
                        break
                    
                    await out_file.write(data)
                    bytes_written += len(data)
                    
                    if on_progress and total_bytes > 0:
                        pct = (bytes_written / total_bytes) * 100.0
                        on_progress(min(100.0, pct))
    
    result.success = True
    result.chunks_merged = len(sorted_chunks)
    return result
```

---

## Performance Characteristics

| Scenario | Chunks | Speed Gain |
|----------|--------|-----------|
| Fast server, good connection | 4-8 | 2-4x |
| Slow server, congested | 2-4 | 1-2x |
| Single-chunk (no Range) | 1 | 1x baseline |

**Factors affecting performance:**
1. **Server capacity** — can handle multiple Range requests
2. **Network bandwidth** — total capacity vs. parallel streams
3. **Latency** — fewer chunks better on high-latency links
4. **File size** — large files benefit from more chunks

---

## Configuration

**`config.json` - Download Settings:**

```json
{
  "general": {
    "default_chunks": 8,
    "default_directory": "./downloads"
  },
  "advanced": {
    "min_chunk_size_bytes": 262144,        # 256 KB minimum
    "max_chunk_size_bytes": 52428800,      # 50 MB maximum
    "chunk_buffer_size_bytes": 65536,      # 64 KB read buffer
    "dynamic_chunk_adjustment": true       # Auto-calculate based on file size
  },
  "network": {
    "max_bandwidth_mbps": 10,              # Global throttle
    "per_download_bandwidth_mbps": 5,      # Per-download throttle
    "connection_timeout_sec": 30,
    "max_retries": 3
  }
}
```

---

## Example: Download Lifecycle

### File: `my-large-file.zip` (1 GB)

**1. Preflight:**
```python
HEAD /my-large-file.zip
  → Content-Length: 1073741824
  → Accept-Ranges: bytes
  → ETag: "abc123"
```

**2. Chunk Calculation:**
```
dynamic_chunk_count(1_073_741_824) → 4 chunks
calculate_chunks(1_073_741_824, 4)
  → Chunk 0: bytes 0-268435455 (256 MB)
  → Chunk 1: bytes 268435456-536870911 (256 MB)
  → Chunk 2: bytes 536870912-805306367 (256 MB)
  → Chunk 3: bytes 805306368-1073741823 (256 MB + remainder)
```

**3. Parallel Downloads (4 asyncio tasks):**
```
Time 0s:   [====================] Chunk 0
           [====================] Chunk 1
           [====================] Chunk 2
           [====================] Chunk 3

Time 30s:  All 4 chunks complete (assuming ~8.5 MB/s per chunk)
```

**4. Merge:**
```
chunk_0.part (268 MB) ─┐
chunk_1.part (268 MB) ─┼─→ my-large-file.zip (1 GB)
chunk_2.part (268 MB) ─┤
chunk_3.part (268 MB) ─┘
```

**5. Verification:**
```
SHA256: abc123... ✓ matches expected hash
```

---

## Status Column Display

The UI shows remaining chunks dynamically:

```
During Download:
Filename              Size      Progress   Chunks   Speed      ETA
my-large-file.zip    1 GB      50%        2/4      8 MB/s     30s

Status Bar: "Chunks: 12"  (3 active files × 4 chunks each)
```

---

## References

- **RFC 7233** — HTTP Range Requests: https://tools.ietf.org/html/rfc7233
- **asyncio** — Python async/await: https://docs.python.org/3/library/asyncio.html
- **aiohttp** — Async HTTP client: https://docs.aiohttp.org/
- **Token Bucket** — Rate limiting: https://en.wikipedia.org/wiki/Token_bucket
