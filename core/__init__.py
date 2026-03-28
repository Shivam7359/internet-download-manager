"""
IDM Core Package
================
The download engine — async workers, queue management, chunk assembly,
persistent storage, and network utilities.

Modules:
    downloader  — Multi-threaded chunk download logic (HTTP/HTTPS/FTP)
    manager     — Download queue, scheduler, concurrency control
    assembler   — Chunk merging and hash verification
    storage     — SQLite wrapper for download state persistence
    network     — Proxy, bandwidth throttle, TLS configuration
    hls_worker  — Authorized HLS playlist/segment worker
"""

__all__ = [
    "downloader",
    "manager",
    "assembler",
    "storage",
    "network",
    "hls_worker",
]
