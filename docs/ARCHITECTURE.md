# Architecture

This document explains the high-level system architecture for IDM.

## Components

- Desktop App (Qt/Python): user interface, queue management, and download lifecycle
- Bridge API (FastAPI/WebSocket): secure local communication endpoint for extension events
- Downloader Engine: multi-chunk transfer workers, retry logic, and speed telemetry
- Storage Layer: metadata persistence, queue state, and transfer history
- Chrome Extension: candidate link detection, scoring, and user-triggered handoff

## Data Flow

1. User browses pages in Chrome.
2. Extension inspects links/media and assigns confidence scores.
3. Extension sends selected payload to local Bridge API.
4. Desktop app validates payload and enqueues downloads.
5. Downloader runs chunked transfers and writes output atomically.
6. UI updates progress, speed, and state through async events.

## Desktop Internals

- Core domain modules handle network IO, assembly, and protocol-specific behavior.
- UI modules focus on dialogs, tray behavior, and visual feedback.
- Server modules expose local HTTP/WebSocket endpoints for extension integration.
- Utility modules include categorization, credentials, clipboard monitoring, and scheduling.

## Fault Tolerance

- Per-download retry with bounded backoff
- Health checks for local bridge state
- Safe resume where supported
- Guardrails around file naming and destination paths

## Extension Integration Model

- Pairing token authorizes extension-to-desktop actions
- Local-only API prevents remote network exposure by default
- Queue actions (start, pause, cancel) are relayed through typed bridge commands

## Packaging

- Windows build uses PowerShell packaging script
- Linux build uses shell script with optional binary packaging pipeline