"""
IDM Utilities — Download Scheduler
====================================
Time-window based download scheduling.

Allows users to define time windows (e.g. 2 AM – 6 AM) during which
downloads should run.  Outside the window, the engine pauses all
downloads and resumes when the window opens.

Features:
    • Multiple schedule rules (weekday-specific or daily)
    • Timezone-aware using system local time
    • Async checker loop for the engine
    • Manual override (force start/stop regardless of schedule)

Usage::

    scheduler = DownloadScheduler(config)
    scheduler.start(engine)    # begins monitoring
    scheduler.stop()

    if scheduler.is_within_window():
        print("Downloads allowed now")
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta
from typing import Any, Optional

log = logging.getLogger("idm.utils.scheduler")


def _parse_hhmm(value: str, fallback: dt_time) -> dt_time:
    """Parse HH:MM into a time value, returning fallback on malformed input."""
    try:
        parts = [int(p) for p in str(value).split(":", maxsplit=1)]
        if len(parts) != 2:
            return fallback
        hour, minute = parts
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return fallback
        return dt_time(hour=hour, minute=minute)
    except (TypeError, ValueError):
        return fallback


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  DATA CLASSES                                                               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@dataclass
class ScheduleRule:
    """
    A single schedule time window.

    Attributes:
        start_time: Start of the allowed window (HH:MM).
        end_time: End of the allowed window (HH:MM).
        days: Days of the week this rule applies to (0=Mon, 6=Sun).
              Empty list means every day.
        enabled: Whether this rule is active.
    """
    start_time: dt_time = field(default_factory=lambda: dt_time(0, 0))
    end_time: dt_time = field(default_factory=lambda: dt_time(23, 59))
    days: list[int] = field(default_factory=list)
    enabled: bool = True

    @property
    def is_overnight(self) -> bool:
        """True if the window spans midnight (e.g. 22:00 → 06:00)."""
        return self.end_time < self.start_time

    def applies_today(self, now: Optional[datetime] = None) -> bool:
        """Check if this rule applies to the current day of the week."""
        if not self.enabled:
            return False
        if not self.days:
            return True  # applies every day
        today = (now or datetime.now()).weekday()
        return today in self.days

    def is_active(self, now: Optional[datetime] = None) -> bool:
        """
        Check if the current time falls within this rule's window.

        Handles overnight windows (e.g. 22:00 → 06:00) correctly.
        """
        if not self.enabled:
            return False
        if not self.applies_today(now):
            return False

        current = (now or datetime.now()).time()

        if self.is_overnight:
            # Window spans midnight: active if AFTER start OR BEFORE end
            return current >= self.start_time or current <= self.end_time
        else:
            # Normal window: active if BETWEEN start and end
            return self.start_time <= current <= self.end_time

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduleRule:
        """Create a ScheduleRule from a config dictionary."""
        start_parts = str(data.get("start_time", "00:00")).split(":")
        end_parts = str(data.get("end_time", "23:59")).split(":")

        return cls(
            start_time=dt_time(int(start_parts[0]), int(start_parts[1])),
            end_time=dt_time(int(end_parts[0]), int(end_parts[1])),
            days=data.get("days", []),
            enabled=data.get("enabled", True),
        )


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  DOWNLOAD SCHEDULER                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class DownloadScheduler:
    """
    Manages download scheduling based on time windows.

    The scheduler checks periodically whether the current time falls
    within any active schedule rule.  If scheduling is enabled and no
    rule matches, downloads are paused.

    Args:
        config: Application configuration dictionary.
        check_interval: Seconds between schedule checks.
    """

    def __init__(
        self,
        config: dict[str, Any],
        check_interval: float = 30.0,
    ) -> None:
        self._config = config
        self._check_interval = check_interval
        self._rules: list[ScheduleRule] = []
        self._enabled: bool = False
        self._override: Optional[bool] = None  # None = no override
        self._task: Optional[asyncio.Task[None]] = None
        self._was_active: bool = True  # tracks state transitions
        self._engine: Optional[Any] = None
        self._scheduler_paused_ids: set[str] = set()

        self._load_config(config)

    def _load_config(self, config: dict[str, Any]) -> None:
        """Load schedule rules from configuration."""
        sched = config.get("scheduler", {})
        self._enabled = sched.get("enabled", False)

        rules_data = sched.get("rules", [])
        if isinstance(rules_data, list):
            self._rules = [ScheduleRule.from_dict(r) for r in rules_data]
        else:
            # Legacy format: single start/end time
            self._rules = [
                ScheduleRule(
                    start_time=_parse_hhmm(str(sched.get("start_time", "00:00")), dt_time(0, 0)),
                    end_time=_parse_hhmm(str(sched.get("end_time", "23:59")), dt_time(23, 59)),
                    enabled=self._enabled,
                )
            ]

        log.info(
            "Scheduler: enabled=%s, rules=%d", self._enabled, len(self._rules)
        )

    @property
    def enabled(self) -> bool:
        """Whether scheduling is enabled."""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value
        log.info("Scheduler %s", "enabled" if value else "disabled")

    @property
    def rules(self) -> list[ScheduleRule]:
        """Current schedule rules."""
        return self._rules.copy()

    def is_within_window(self, now: Optional[datetime] = None) -> bool:
        """
        Check if downloads are allowed right now.

        Logic:
            1. If override is set, return override value.
            2. If scheduling is disabled, always allow.
            3. If any active rule matches the current time, allow.
            4. Otherwise, disallow.
        """
        if self._override is not None:
            return self._override

        if not self._enabled:
            return True

        if not self._rules:
            return True

        return any(rule.is_active(now) for rule in self._rules)

    def set_override(self, allow: Optional[bool]) -> None:
        """
        Set a manual override.

        Args:
            allow: True = force allow, False = force block, None = use schedule.
        """
        self._override = allow
        if allow is None:
            log.info("Schedule override cleared")
        else:
            log.info("Schedule override: %s", "allow" if allow else "block")

    def add_rule(self, rule: ScheduleRule) -> None:
        """Add a schedule rule."""
        self._rules.append(rule)

    def clear_rules(self) -> None:
        """Remove all schedule rules."""
        self._rules.clear()

    def reload_config(self, config: dict[str, Any]) -> None:
        """Reload scheduler state from updated app configuration."""
        self._config = config
        self._load_config(config)

    def time_until_window(self, now: Optional[datetime] = None) -> Optional[float]:
        """
        Calculate seconds until the next schedule window opens.

        Returns None if scheduling is disabled or we're already in a window.
        """
        if not self._enabled or self.is_within_window(now):
            return None

        current = now or datetime.now()

        min_wait = float("inf")
        for rule in self._rules:
            if not rule.enabled:
                continue
            if not rule.applies_today(current):
                continue

            # Calculate seconds until this rule's start_time
            start_dt = current.replace(
                hour=rule.start_time.hour,
                minute=rule.start_time.minute,
                second=0, microsecond=0,
            )
            if start_dt <= current:
                # Start time is in the past today — try tomorrow
                start_dt = start_dt + timedelta(days=1)

            diff = (start_dt - current).total_seconds()
            min_wait = min(min_wait, diff)

        return min_wait if min_wait < float("inf") else None

    # ── Async Loop ─────────────────────────────────────────────────────────

    async def start(self, engine: Any) -> None:
        """
        Start the scheduler monitoring loop.

        Pauses/resumes the engine based on schedule windows.

        Args:
            engine: The DownloadEngine instance (must have
                     pause_all() and resume_all() methods).
        """
        if self._task and not self._task.done():
            return

        self._engine = engine
        await self._apply_schedule_state(engine)

        self._task = asyncio.create_task(
            self._monitor_loop(), name="scheduler"
        )
        log.info("Scheduler started")

    async def stop(self) -> None:
        """Stop the scheduler monitoring loop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._engine is not None and self._scheduler_paused_ids:
            try:
                managed_ids = set(self._scheduler_paused_ids)
                await self._engine.resume_downloads(managed_ids)
            except Exception:
                log.exception("Failed to resume scheduler-managed paused downloads on stop")
            finally:
                self._scheduler_paused_ids.clear()

        self._engine = None
        log.info("Scheduler stopped")

    async def _monitor_loop(self) -> None:
        """Periodically check schedule and pause/resume engine."""
        while True:
            try:
                if self._engine is not None:
                    await self._apply_schedule_state(self._engine)

            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Scheduler error")

            await asyncio.sleep(self._check_interval)

    async def _apply_schedule_state(self, engine: Any) -> None:
        """Apply one scheduler state transition against the engine."""
        allowed = self.is_within_window()

        if allowed and not self._was_active:
            log.info("Schedule window opened — resuming downloads")
            if self._scheduler_paused_ids:
                managed_ids = set(self._scheduler_paused_ids)
                await engine.resume_downloads(managed_ids)
                self._scheduler_paused_ids.clear()
            self._was_active = True

        elif not allowed and self._was_active:
            log.info("Schedule window closed — pausing downloads")
            paused_ids = await engine.pause_all()
            self._scheduler_paused_ids.update(paused_ids)
            self._was_active = False
