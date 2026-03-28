"""
IDM Server Package
==================
FastAPI bridge server for browser-extension communication.

The browser extension sends download jobs via REST API and receives
real-time progress updates over WebSocket.

Endpoints:
    POST /add       — Add a new download job
    GET  /status    — List active downloads with progress
    WS   /ws        — Real-time speed / progress push

Modules:
    api        — FastAPI application, REST endpoints, CORS
    websocket  — WebSocket manager for real-time updates
"""

__all__ = [
    "api",
    "websocket",
]
