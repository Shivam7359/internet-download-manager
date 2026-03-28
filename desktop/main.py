"""Desktop app entrypoint wrapper.

This keeps a clean public folder layout while delegating to the existing
application entrypoint.
"""

from main import main as app_main  # type: ignore[attr-defined]

if __name__ == "__main__":
    app_main()
