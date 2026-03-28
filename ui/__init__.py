"""
IDM UI Package
==============
PyQt6 user interface components — main window, download queue panel,
settings dialog, system-tray icon, and real-time speed graph.

Modules:
    main_window      — Primary application window with toolbar and status bar
    queue_panel      — Table view of active / queued / completed downloads
    settings_dialog  — Configuration dialog (General, Network, Scheduler, etc.)
    tray_icon        — System tray icon with context menu and notifications
    speed_graph      — Real-time download speed chart (pyqtgraph)
"""

__all__ = [
    "main_window",
    "queue_panel",
    "settings_dialog",
    "tray_icon",
    "speed_graph",
]
