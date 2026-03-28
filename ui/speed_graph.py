"""
IDM UI — Real-Time Speed Graph
================================
A custom QPainter-based line chart showing download speed over time.

Features:
    • Smooth animated line with gradient fill
    • Auto-scaling Y axis
    • Configurable sample count and update interval
    • Grid lines and axis labels
    • Peak speed indicator
    • No external dependencies (pure QPainter)

Usage::

    graph = SpeedGraphWidget()
    graph.add_sample(speed_bps)   # call periodically
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Optional

from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF, QSize
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QLinearGradient,
    QFont, QPainterPath, QPolygonF, QPaintEvent,
)
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy

from core.network import format_speed

log = logging.getLogger("idm.ui.speed_graph")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  SPEED GRAPH WIDGET                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class SpeedGraphWidget(QWidget):
    """
    Real-time download speed chart.

    Renders a smooth line graph with gradient fill showing the
    aggregate download speed over time.

    Args:
        max_samples: Number of data points to display.
        parent: Parent widget.
    """

    def __init__(
        self,
        max_samples: int = 60,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._max_samples = max_samples
        self._samples: deque[float] = deque([0.0] * max_samples, maxlen=max_samples)
        self._peak_speed: float = 0.0
        self._current_speed: float = 0.0

        self.setMinimumSize(300, 120)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )

        # Colors
        self._bg_color = QColor("#0D1117")
        self._grid_color = QColor("#1F2937")
        self._line_color = QColor("#3B82F6")
        self._fill_top = QColor(59, 130, 246, 80)
        self._fill_bottom = QColor(59, 130, 246, 5)
        self._text_color = QColor("#8B949E")
        self._peak_color = QColor("#F59E0B")
        self._label_font = QFont("Segoe UI", 9)

        # Margins (left for Y-axis labels, bottom for time)
        self._margin_left = 70
        self._margin_right = 16
        self._margin_top = 12
        self._margin_bottom = 24

    def add_sample(self, speed_bps: float) -> None:
        """Add a new speed sample and trigger repaint."""
        self._samples.append(speed_bps)
        self._current_speed = speed_bps
        if speed_bps > self._peak_speed:
            self._peak_speed = speed_bps
        self.update()

    def reset(self) -> None:
        """Clear all samples and peak."""
        self._samples = deque([0.0] * self._max_samples, maxlen=self._max_samples)
        self._peak_speed = 0.0
        self._current_speed = 0.0
        self.update()

    @property
    def peak_speed(self) -> float:
        return self._peak_speed

    @property
    def current_speed(self) -> float:
        return self._current_speed

    # ── Painting ───────────────────────────────────────────────────────────

    def paintEvent(self, event: QPaintEvent | None) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        chart_x = self._margin_left
        chart_y = self._margin_top
        chart_w = w - self._margin_left - self._margin_right
        chart_h = h - self._margin_top - self._margin_bottom

        if chart_w < 10 or chart_h < 10:
            painter.end()
            return

        # Background
        painter.fillRect(0, 0, w, h, self._bg_color)

        # Chart area border
        painter.setPen(QPen(self._grid_color, 1))
        painter.drawRect(chart_x, chart_y, chart_w, chart_h)

        # Y-axis scale
        max_val = max(max(self._samples), 1024.0)
        # Round up to nice number
        max_val = self._nice_max(max_val)

        # Grid lines (horizontal)
        grid_lines = 4
        painter.setFont(self._label_font)
        for i in range(grid_lines + 1):
            y = chart_y + chart_h - (i / grid_lines) * chart_h
            val = (i / grid_lines) * max_val

            # Grid line
            painter.setPen(QPen(self._grid_color, 1, Qt.PenStyle.DotLine))
            painter.drawLine(int(chart_x), int(y), int(chart_x + chart_w), int(y))

            # Label
            painter.setPen(self._text_color)
            label = format_speed(val) if val > 0 else "0"
            painter.drawText(
                QRectF(0, y - 10, self._margin_left - 8, 20),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                label,
            )

        # Data points
        samples = list(self._samples)
        n = len(samples)
        if n < 2:
            painter.end()
            return

        points: list[QPointF] = []
        for i, val in enumerate(samples):
            x = chart_x + (i / (n - 1)) * chart_w
            y = chart_y + chart_h - (val / max_val) * chart_h
            points.append(QPointF(x, y))

        # Fill area under curve
        fill_path = QPainterPath()
        fill_path.moveTo(QPointF(chart_x, chart_y + chart_h))
        for p in points:
            fill_path.lineTo(p)
        fill_path.lineTo(QPointF(chart_x + chart_w, chart_y + chart_h))
        fill_path.closeSubpath()

        gradient = QLinearGradient(0, chart_y, 0, chart_y + chart_h)
        gradient.setColorAt(0, self._fill_top)
        gradient.setColorAt(1, self._fill_bottom)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawPath(fill_path)

        # Line
        line_path = QPainterPath()
        line_path.moveTo(points[0])
        for p in points[1:]:
            line_path.lineTo(p)
        painter.setPen(QPen(self._line_color, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(line_path)

        # Current speed dot
        if points:
            last = points[-1]
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._line_color)
            painter.drawEllipse(last, 4, 4)

        # Peak line
        if self._peak_speed > 0:
            peak_y = chart_y + chart_h - (self._peak_speed / max_val) * chart_h
            if chart_y <= peak_y <= chart_y + chart_h:
                painter.setPen(QPen(self._peak_color, 1, Qt.PenStyle.DashLine))
                painter.drawLine(
                    int(chart_x), int(peak_y),
                    int(chart_x + chart_w), int(peak_y),
                )
                painter.setPen(self._peak_color)
                painter.drawText(
                    QRectF(chart_x + chart_w - 120, peak_y - 16, 120, 14),
                    Qt.AlignmentFlag.AlignRight,
                    f"Peak: {format_speed(self._peak_speed)}",
                )

        # Time labels
        painter.setPen(self._text_color)
        painter.drawText(
            QRectF(chart_x, chart_y + chart_h + 4, 60, 16),
            Qt.AlignmentFlag.AlignLeft,
            f"-{self._max_samples}s",
        )
        painter.drawText(
            QRectF(chart_x + chart_w - 40, chart_y + chart_h + 4, 40, 16),
            Qt.AlignmentFlag.AlignRight,
            "Now",
        )

        painter.end()

    @staticmethod
    def _nice_max(value: float) -> float:
        """Round up to a 'nice' number for axis scaling."""
        if value <= 0:
            return 1024.0

        # Find order of magnitude
        import math
        exp = math.floor(math.log10(value))
        base = 10 ** exp
        fraction = value / base

        if fraction <= 1.0:
            nice = 1.0
        elif fraction <= 2.0:
            nice = 2.0
        elif fraction <= 5.0:
            nice = 5.0
        else:
            nice = 10.0

        return float(nice * base)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  SPEED PANEL (Graph + Stats)                                                ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class SpeedPanel(QWidget):
    """
    Combined speed graph with current/peak/average stats labels.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background-color: #161B22;")  # Dark background
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Stats row
        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(24)

        self._current_label = self._make_stat_label("Current", "0 B/s")
        self._peak_label = self._make_stat_label("Peak", "0 B/s")
        self._active_label = self._make_stat_label("Active", "0")

        stats_layout.addWidget(self._current_label)
        stats_layout.addWidget(self._peak_label)
        stats_layout.addWidget(self._active_label)
        stats_layout.addStretch()
        layout.addLayout(stats_layout)

        # Graph
        self._graph = SpeedGraphWidget(max_samples=60, parent=self)
        self._graph.setMinimumHeight(100)
        layout.addWidget(self._graph)

    @property
    def graph(self) -> SpeedGraphWidget:
        return self._graph

    def update_stats(
        self, current_speed: float, active_count: int
    ) -> None:
        """Update the stats labels and add a sample to the graph."""
        self._graph.add_sample(current_speed)

        self._current_label.findChild(QLabel, "value").setText(
            format_speed(current_speed)
        )
        self._peak_label.findChild(QLabel, "value").setText(
            format_speed(self._graph.peak_speed)
        )
        self._active_label.findChild(QLabel, "value").setText(
            str(active_count)
        )

    @staticmethod
    def _make_stat_label(title: str, initial: str) -> QWidget:
        widget = QWidget()
        widget.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            "font-size: 10px; color: #6B7280; font-weight: 600; "
            "text-transform: uppercase; background: transparent;"
        )
        layout.addWidget(title_lbl)

        value_lbl = QLabel(initial)
        value_lbl.setObjectName("value")
        value_lbl.setStyleSheet(
            "font-size: 16px; color: #E5E7EB; font-weight: 700; "
            "background: transparent;"
        )
        layout.addWidget(value_lbl)

        return widget
