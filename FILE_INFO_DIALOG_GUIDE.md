"""
IDM File Info Preview Feature Guide
====================================

OVERVIEW:
---------
Added a comprehensive file info preview system to IDM that displays file details
before downloading and provides a refresh mechanism to re-fetch file information.

NEW FEATURES:
=============

1. FILE INFO PREVIEW DIALOG
   ─────────────────────────
   - Displays when a user starts a download
   - Shows file metadata: name, size, content type, resume support
   - Allows category selection before download
   - Has three action buttons:
     * "Start Download" - Begin download immediately
     * "Download Later" - Queue for later start
     * "Refresh" - Re-fetch file info from server
     * "Cancel" - Abort the download add operation

2. REFRESH OPTION IN DOWNLOADS LIST
   ──────────────────────────────────
   - Available via right-click context menu on any download
   - Shows the file info dialog for the selected download
   - Allows checking if file info has changed since queuing
   - Works for downloads in any status (queued, downloading, paused, etc.)

3. ENHANCED ADD DOWNLOAD FLOW
   ───────────────────────────
   Step 1: User clicks "Add URL" or uses Ctrl+N
   Step 2: AddDownloadDialog opens (existing)
   Step 3: User enters URL and clicks "Start Download"
   Step 4: FileInfoDialog opens showing file details
   Step 5: User reviews info and chooses action:
           - Start Download: begin immediately
           - Download Later: queue for later
   Step 6: Download is added to list

FILE INFO DISPLAYED:
====================
- URL: The resolved URL (with preview truncation if too long)
- Filename: Auto-detected from server response
- File Size: Formatted human-readable size
- Content Type: MIME type from server
- Resume Support: Whether server supports partial downloads
- Save Location: Directory where file will be saved
- Category: Auto-detected or user-selected

BACKGROUND THREADING:
=====================
- File info fetching runs in a separate worker thread
- UI remains responsive during network requests
- Automatic timeout handling for slow/unreachable servers
- Graceful error messages if fetch fails

UI/UX FEATURES:
===============
- Modern dark theme matching IDM design
- Indeterminate progress bar during info fetching
- Visually clear buttons: primary (Start Download) vs secondary (Cancel)
- Color-coded resume support indicator (green for yes, red for no)
- Auto-detection of file category from filename
- Professional dialog layout with sections

INTEGRATION POINTS:
===================
- ui/add_dialog.py: Modified _on_start() to show FileInfoDialog
- ui/main_window.py: Added context menu "Refresh File Info" option
- ui/file_info_dialog.py: New FileInfoDialog class (380+ lines)
- New PreflightThread worker class for async networking
- Signal-based communication between dialogs

USAGE EXAMPLES:
===============

Example 1: Adding a download with preview
──────────────────────────────────────────
User Action: "Add URL"
↓
User enters: https://example.com/large-video.mp4
↓
User clicks: "Start Download"
↓
FileInfoDialog displays:
  - URL: https://example.com/large-video.mp4
  - Filename: large-video.mp4
  - Size: 1.5 GB
  - Type: video/mp4
  - Resume: ✓ Yes (green)
  - Save to: D:\idm down\Video
  - Category: Video (auto-selected)
↓
User clicks: "Start Download"
↓
Download added to queue and starts immediately

Example 2: Refreshing file info for existing download
──────────────────────────────────────────────────────
User Action: Right-click on queued download
↓
User selects: "Refresh File Info"
↓
FileInfoDialog opens showing current server info
↓
User can see if filename, size, or resumability changed
↓
Close dialog to continue

ERROR HANDLING:
===============
- Network timeouts: "Error fetching file info: [timeout message]"
- Invalid URLs: Dialog shows appropriate error message
- Unreachable servers: Clear error with retry option (Refresh button)
- Missing file info: Shows "Unknown" for unavailable fields

CONFIGURATION:
===============
No additional configuration needed. Uses existing:
- Download directory from config
- Network settings (proxy, SSL, etc.)
- Category auto-detection rules

TESTING:
========
Unit test available: tests/test_file_info_dialog.py
- Tests dialog UI creation
- Tests preflight info fetching
- Tests signal connectivity

FUTURE ENHANCEMENTS:
====================
1. Add estimated download time based on speed
2. Show file preview thumbnail for images
3. Add "Save As" option in FileInfoDialog
4. Batch refresh for multiple downloads
5. Add file validation checksum display
6. Schedule download after fetching metadata
"""
