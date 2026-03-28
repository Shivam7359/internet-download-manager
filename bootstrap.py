"""
IDM Bootstrap Module
====================
Startup/shutdown orchestration primitives extracted from main.py.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Any, Callable, Optional


class EngineThread(threading.Thread):
    """Dedicated thread hosting the asyncio event loop for download runtime."""

    def __init__(self) -> None:
        super().__init__(name="IDM-EngineThread", daemon=True)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready_event = threading.Event()
        self._log = logging.getLogger("idm.engine_thread")

    def run(self) -> None:
        self._log.info("Engine thread starting...")
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        self._ready_event.set()
        self._log.info("Engine event loop running")

        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            if pending:
                self._log.info("Cancelling %d pending tasks...", len(pending))
                for task in pending:
                    task.cancel()
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()
            self._log.info("Engine event loop closed")

    def wait_ready(self, timeout: float = 10.0) -> bool:
        return self._ready_event.wait(timeout)

    def stop(self) -> None:
        if self.loop and self.loop.is_running():
            self._log.info("Stopping engine event loop...")
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.join(timeout=10)

    def run_coroutine(self, coro: Any) -> Any:
        if not self.loop or not self.loop.is_running():
            raise RuntimeError("Engine loop is not running")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


class ServerThread(threading.Thread):
    """Dedicated thread for the FastAPI/uvicorn bridge server."""

    def __init__(self, host: str = "127.0.0.1", port: int = 6800) -> None:
        super().__init__(name="IDM-ServerThread", daemon=True)
        self.host = host
        self.port = port
        self._server: Optional[Any] = None
        self._log = logging.getLogger("idm.server_thread")
        self._ready_event = threading.Event()

    def run(self) -> None:
        self._log.info("Server thread starting on %s:%d ...", self.host, self.port)
        try:
            import uvicorn

            try:
                import server.api as api_module
                if hasattr(api_module, "app"):
                    app = api_module.app
                    self._log.info("Loaded patched FastAPI app from server.api")
                else:
                    app = api_module.create_app()
                    self._log.info("Created default FastAPI app from server.api")
            except ImportError:
                self._log.warning(
                    "server.api not found - starting placeholder app. "
                    "Build server/api.py to enable the bridge."
                )
                from fastapi import FastAPI
                app = FastAPI(title="IDM Bridge (placeholder)")

                @app.get("/status")
                async def _placeholder_status() -> dict[str, Any]:
                    return {"status": "ok", "placeholder": True, "downloads": []}

            config = uvicorn.Config(
                app=app,
                host=self.host,
                port=self.port,
                log_level="warning",
                access_log=False,
                log_config=None,
            )
            self._server = uvicorn.Server(config)
            self._ready_event.set()
            self._server.run()
        except Exception:
            self._log.exception("Server thread failed")
            self._ready_event.set()

    def wait_ready(self, timeout: float = 10.0) -> bool:
        return self._ready_event.wait(timeout)

    def stop(self) -> None:
        if self._server:
            self._log.info("Stopping bridge server...")
            self._server.should_exit = True
        self.join(timeout=10)


async def init_subsystems(storage: Any, network: Any) -> None:
    """Initialize storage and network on the engine event loop."""
    await storage.initialize()
    await network.initialize()


async def shutdown_subsystems(storage: Any, network: Any) -> None:
    """Gracefully close storage and network."""
    try:
        await network.close()
    except Exception:
        logging.getLogger("idm").warning("Network shutdown error", exc_info=True)
    try:
        await storage.close()
    except Exception:
        logging.getLogger("idm").warning("Storage shutdown error", exc_info=True)


def install_shutdown_diagnostics(app: Any, window: Any, log: logging.Logger) -> Callable[[], str]:
    """Install diagnostics that capture likely shutdown triggers for debugging."""
    state: dict[str, str] = {"reason": "unknown"}

    def mark(reason: str) -> None:
        if state["reason"] == "unknown":
            state["reason"] = reason
            log.warning("Shutdown reason marked: %s", reason)

    try:
        app.lastWindowClosed.connect(lambda: mark("lastWindowClosed signal emitted"))
        app.aboutToQuit.connect(
            lambda: log.warning(
                "QApplication aboutToQuit fired (reason=%s, window_visible=%s, window_minimized=%s)",
                state["reason"],
                bool(window.isVisible()),
                bool(window.isMinimized()),
            )
        )
    except Exception:
        log.warning("Failed to attach Qt shutdown diagnostics", exc_info=True)

    original_excepthook = sys.excepthook

    def _exception_hook(exc_type: type[BaseException], exc_value: BaseException, exc_tb: Any) -> None:
        mark("uncaught_exception_main_thread")
        log.critical("Uncaught exception in main thread", exc_info=(exc_type, exc_value, exc_tb))
        original_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _exception_hook

    if hasattr(threading, "excepthook"):
        original_thread_hook = threading.excepthook

        def _thread_exception_hook(args: threading.ExceptHookArgs) -> None:
            mark(f"uncaught_exception_thread:{args.thread.name if args.thread else 'unknown'}")
            exc_type = args.exc_type or Exception
            exc_value = args.exc_value or Exception("Unknown thread exception")
            log.critical("Uncaught exception in thread", exc_info=(exc_type, exc_value, args.exc_traceback))
            original_thread_hook(args)

        threading.excepthook = _thread_exception_hook

    return lambda: state["reason"]


def setup_server_app(engine: Any, storage: Any, config: dict[str, Any]) -> Any:
    """Build and patch the FastAPI app with websocket bridge wiring."""
    import server.api as api_module
    from server.websocket import ConnectionManager, register_websocket

    app = api_module.create_app(engine=engine, storage=storage, config=config)
    ws_manager = ConnectionManager()
    register_websocket(app, ws_manager)
    api_module.app = app  # type: ignore[attr-defined]
    return ws_manager
