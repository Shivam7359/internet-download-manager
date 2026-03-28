"""Simple MIME helper for desktop module layout."""

import mimetypes


def guess_mime_type(filename: str) -> str:
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or "application/octet-stream"
