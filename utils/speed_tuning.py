"""
Speed tuning helpers for stable download throughput.
"""

from __future__ import annotations

from typing import Any


def compute_stable_limits(
    *,
    max_concurrent_downloads: int,
    default_chunks: int,
    bandwidth_limit_kbps: int,
    per_download_bandwidth_kbps: int,
) -> tuple[int, int, str]:
    """
    Compute conservative limits to reduce speed oscillation.

    Returns:
        (global_limit_kbps, per_download_limit_kbps, optional_tip)
    """
    concurrent = max(1, int(max_concurrent_downloads or 1))
    chunks = max(1, int(default_chunks or 1))
    current_global = max(0, int(bandwidth_limit_kbps or 0))
    current_per_download = max(0, int(per_download_bandwidth_kbps or 0))

    if current_global > 0:
        suggested_global = max(256, int(current_global * 0.85))
    elif current_per_download > 0:
        suggested_global = max(256, int(current_per_download * concurrent * 0.9))
    else:
        # Fallback profile when no baseline cap is configured.
        suggested_global = max(1024, concurrent * 1500)

    suggested_per_download = max(128, int(suggested_global / concurrent))
    suggested_per_download = min(suggested_per_download, suggested_global)

    tip = ""
    if concurrent >= 4 and chunks >= 5:
        tip = "Tip: set Default Chunks to 4 for smoother long downloads."

    return suggested_global, suggested_per_download, tip


def apply_stable_limits_to_config(config: dict[str, Any]) -> tuple[bool, int, int, str]:
    """
    Apply computed stable limits directly into ``config``.

    Returns:
        (changed, global_limit_kbps, per_download_limit_kbps, optional_tip)
    """
    general = config.setdefault("general", {})
    network = config.setdefault("network", {})

    suggested_global, suggested_per_download, tip = compute_stable_limits(
        max_concurrent_downloads=int(general.get("max_concurrent_downloads", 4) or 4),
        default_chunks=int(general.get("default_chunks", 5) or 5),
        bandwidth_limit_kbps=int(network.get("bandwidth_limit_kbps", 0) or 0),
        per_download_bandwidth_kbps=int(network.get("per_download_bandwidth_kbps", 0) or 0),
    )

    old_global = int(network.get("bandwidth_limit_kbps", 0) or 0)
    old_per_download = int(network.get("per_download_bandwidth_kbps", 0) or 0)

    changed = (old_global != suggested_global) or (old_per_download != suggested_per_download)
    network["bandwidth_limit_kbps"] = suggested_global
    network["per_download_bandwidth_kbps"] = suggested_per_download

    return changed, suggested_global, suggested_per_download, tip
