"""
IDM Utilities — Auto File Categoriser
=======================================
Automatically categorises downloaded files based on extension and MIME type.

Categories:
    • Video — .mp4, .mkv, .avi, .webm, .mov, .flv, .wmv, …
    • Audio — .mp3, .flac, .wav, .aac, .ogg, .wma, …
    • Image — .jpg, .png, .gif, .webp, .svg, .bmp, .ico, …
    • Document — .pdf, .doc, .xls, .ppt, .txt, .csv, .epub, …
    • Software — .exe, .msi, .dmg, .deb, .rpm, .apk, …
    • Archive — .zip, .rar, .7z, .tar, .gz, .bz2, .xz, …
    • Other — anything not matching the above

Usage::

    category = categorise_file("movie.mkv")          # → "Video"
    category = categorise_by_mime("application/pdf")  # → "Document"
    save_dir = get_category_directory(config, "Video")
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("idm.utils.categoriser")

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  EXTENSION → CATEGORY MAPPING                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

_VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".avi", ".webm", ".mov", ".flv", ".wmv", ".m4v",
    ".mpg", ".mpeg", ".3gp", ".3g2", ".ts", ".mts", ".m2ts", ".vob",
    ".ogv", ".divx", ".asf", ".rm", ".rmvb", ".f4v",
})

_AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".flac", ".wav", ".aac", ".ogg", ".wma", ".m4a", ".opus",
    ".aiff", ".alac", ".ape", ".mid", ".midi", ".mka", ".ac3", ".dts",
    ".amr", ".ra", ".pcm",
})

_IMAGE_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".ico",
    ".tiff", ".tif", ".psd", ".raw", ".cr2", ".nef", ".heic", ".heif",
    ".avif", ".jxl",
})

_DOCUMENT_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".rtf", ".odt", ".ods", ".odp", ".csv", ".tsv",
    ".epub", ".mobi", ".azw", ".azw3", ".djvu", ".fb2",
    ".tex", ".md", ".json", ".xml", ".yaml", ".yml", ".html", ".htm",
    ".log", ".ini", ".cfg", ".conf",
})

_SOFTWARE_EXTENSIONS = frozenset({
    ".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".apk", ".aab",
    ".app", ".bat", ".cmd", ".sh", ".ps1", ".jar", ".appimage",
    ".snap", ".flatpak", ".run", ".bin", ".com",
})

_ARCHIVE_EXTENSIONS = frozenset({
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst",
    ".lz", ".lzma", ".cab", ".iso", ".img", ".dmg", ".tgz",
    ".tbz2", ".txz", ".war", ".ear",
})

# Combined lookup: extension → category
_EXTENSION_MAP: dict[str, str] = {}
for _ext in _VIDEO_EXTENSIONS:
    _EXTENSION_MAP[_ext] = "Video"
for _ext in _AUDIO_EXTENSIONS:
    _EXTENSION_MAP[_ext] = "Audio"
for _ext in _IMAGE_EXTENSIONS:
    _EXTENSION_MAP[_ext] = "Image"
for _ext in _DOCUMENT_EXTENSIONS:
    _EXTENSION_MAP[_ext] = "Document"
for _ext in _SOFTWARE_EXTENSIONS:
    _EXTENSION_MAP[_ext] = "Software"
for _ext in _ARCHIVE_EXTENSIONS:
    _EXTENSION_MAP[_ext] = "Archive"

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  MIME → CATEGORY MAPPING                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

_MIME_PREFIX_MAP: dict[str, str] = {
    "video/": "Video",
    "audio/": "Audio",
    "image/": "Image",
    "text/": "Document",
}

_MIME_EXACT_MAP: dict[str, str] = {
    "application/pdf": "Document",
    "application/msword": "Document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Document",
    "application/vnd.ms-excel": "Document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Document",
    "application/vnd.ms-powerpoint": "Document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "Document",
    "application/epub+zip": "Document",
    "application/json": "Document",
    "application/xml": "Document",
    "application/zip": "Archive",
    "application/x-rar-compressed": "Archive",
    "application/x-7z-compressed": "Archive",
    "application/gzip": "Archive",
    "application/x-bzip2": "Archive",
    "application/x-xz": "Archive",
    "application/x-tar": "Archive",
    "application/vnd.android.package-archive": "Software",
    "application/x-msdownload": "Software",
    "application/x-msi": "Software",
    "application/x-apple-diskimage": "Software",
    "application/x-iso9660-image": "Archive",
    "application/octet-stream": "Other",
}

ALL_CATEGORIES = ("Video", "Audio", "Image", "Document", "Software", "Archive", "Other")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  PUBLIC API                                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def categorise_file(filename: str) -> str:
    """
    Categorise a file based on its extension.

    Args:
        filename: The filename (basename or full path).

    Returns:
        Category string: Video, Audio, Image, Document, Software, Archive, or Other.
    """
    ext = Path(filename).suffix.lower()

    # Handle compound extensions
    lower = filename.lower()
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if lower.endswith(compound):
            return "Archive"

    return _EXTENSION_MAP.get(ext, "Other")


def categorise_by_mime(content_type: str) -> str:
    """
    Categorise a file based on its MIME type.

    Args:
        content_type: The Content-Type header value (may include params).

    Returns:
        Category string.
    """
    if not content_type:
        return "Other"

    # Strip parameters (e.g., "text/html; charset=utf-8" → "text/html")
    mime = content_type.split(";")[0].strip().lower()

    # Exact match
    if mime in _MIME_EXACT_MAP:
        return _MIME_EXACT_MAP[mime]

    # Prefix match
    for prefix, category in _MIME_PREFIX_MAP.items():
        if mime.startswith(prefix):
            return category

    return "Other"


def categorise(
    filename: str,
    content_type: Optional[str] = None,
) -> str:
    """
    Categorise a file using both extension and MIME type.

    Extension takes precedence over MIME type since it's more specific.

    Args:
        filename: The filename.
        content_type: Optional Content-Type header.

    Returns:
        Category string.
    """
    cat = categorise_file(filename)
    if cat != "Other":
        return cat

    if content_type:
        return categorise_by_mime(content_type)

    return "Other"


def get_category_directory(
    config: dict[str, Any],
    category: str,
) -> Path:
    """
    Get the download directory for a specific category.

    Reads from ``config["categories"]`` mapping. Falls back to the
    general download directory with category as subfolder.

    Args:
        config: Application configuration dict.
        category: The file category.

    Returns:
        Absolute path to the category download directory.
    """
    categories = config.get("categories", {})
    base_dir = Path(
        config.get("general", {}).get(
            "download_directory",
            str(Path.home() / "Downloads" / "IDM"),
        )
    )

    # Check if category has a custom directory
    cat_config = categories.get(category.lower(), {})
    if isinstance(cat_config, dict):
        custom_dir = cat_config.get("directory", "")
        if custom_dir:
            return Path(custom_dir)

    return base_dir / category


def get_all_extensions(category: str) -> frozenset[str]:
    """
    Get all file extensions for a given category.

    Args:
        category: The category name (case-insensitive).

    Returns:
        Frozenset of extensions (with leading dot).
    """
    cat_map = {
        "video": _VIDEO_EXTENSIONS,
        "audio": _AUDIO_EXTENSIONS,
        "image": _IMAGE_EXTENSIONS,
        "document": _DOCUMENT_EXTENSIONS,
        "software": _SOFTWARE_EXTENSIONS,
        "archive": _ARCHIVE_EXTENSIONS,
    }
    return cat_map.get(category.lower(), frozenset())
