"""Tiny server-side SVG charts: no JavaScript, no third-party libraries.

Colors come from CSS classes in app.css (inline styles would violate the CSP).
"""

from __future__ import annotations

import math
from datetime import date

from markupsafe import Markup, escape

from budgetapp.dates import month_label
from budgetapp.money import format_money


def compact_money(cents: int) -> str:
    dollars = abs(cents) / 100  # display only
    sign = "-" if cents < 0 else ""
    if dollars >= 1_000_000:
        return f"{sign}${dollars / 1_000_000:.1f}M"
    if dollars >= 10_000:
        return f"{sign}${dollars / 1_000:.0f}k"
    if dollars >= 1_000:
        return f"{sign}${dollars / 1_000:.1f}k"
    return f"{sign}${dollars:.0f}"


def donut(
    parts: list[tuple[int, str]], *, total: int, value: str, caption: str, size: int = 190
) -> Markup:
    """Ring chart. `parts` are (amount, css class) arcs laid end to end around a track
    representing `total`; anything past the total is clipped."""
    radius, stroke = 70, 16
    circumference = 2 * math.pi * radius
    centre = 90
    pieces = [
        f'<svg class="chart donut" viewBox="0 0 180 180" width="{size}" height="{size}" '
        f'role="img" aria-label="{escape(caption)}: {escape(value)}">',
        f'<circle class="donut-track" cx="{centre}" cy="{centre}" r="{radius}" '
        f'stroke-width="{stroke}"/>',
        f'<g transform="rotate(-90 {centre} {centre})">',
    ]
    used = 0.0
    for amount, css in parts:
        if total <= 0 or amount <= 0 or used >= 1:
            continue
        share = min(amount / total, 1 - used)
        length = max(circumference * share - 3, 0.1)  # small gap between arcs
        pieces.append(
            f'<circle class="{escape(css)}" cx="{centre}" cy="{centre}" r="{radius}" '
            f'stroke-width="{stroke}" stroke-dasharray="{length:.1f} {circumference:.1f}" '
            f'stroke-dashoffset="{-circumference * used:.1f}"/>'
        )
        used += share
    pieces.append("</g>")
    pieces.append(
        f'<text class="donut-value" x="{centre}" y="{centre + 4}" text-anchor="middle">'
        f"{escape(value)}</text>"
        f'<text class="donut-caption" x="{centre}" y="{centre + 24}" text-anchor="middle">'
        f"{escape(caption)}</text></svg>"
    )
    return Markup("".join(pieces))


SERIES_COLORS = 8  # CSS classes series-0 .. series-7


def stacked_bars(
    months: list[date],
    series: list[tuple[str, list[int]]],
    *,
    width: int = 760,
    height: int = 260,
    label: str = "Chart",
) -> Markup:
    """Stacked monthly columns. `series` is [(name, cents per month)] in stacking order,
    bottom first; series N gets CSS class series-N for its color. Negatives count as 0."""
    if not months or not series:
        return Markup("")
    left, right, top, bottom = 58, 12, 12, 26
    plot = height - top - bottom
    totals = [sum(max(values[i], 0) for _, values in series) for i in range(len(months))]
    hi = max(totals) or 100
    slot = (width - left - right) / len(months)
    bar = slot * 0.62
    every = 1 if len(months) <= 12 else 2 if len(months) <= 24 else 3
    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(label)}">'
    ]
    for frac in (0, 0.5, 1):
        gy = top + plot * (1 - frac)
        parts.append(
            f'<line class="chart-grid" x1="{left}" x2="{width - right}" '
            f'y1="{gy:.1f}" y2="{gy:.1f}"/>'
            f'<text class="chart-label" x="{left - 8}" y="{gy + 4:.1f}" text-anchor="end">'
            f"{escape(compact_money(round(hi * frac)))}</text>"
        )
    for i, month in enumerate(months):
        x = left + i * slot + (slot - bar) / 2
        y = top + plot
        for n, (name, values) in enumerate(series):
            value = max(values[i], 0)
            if not value:
                continue
            h = value / hi * plot
            y -= h
            parts.append(
                f'<rect class="series-{n % SERIES_COLORS}" x="{x:.1f}" y="{y:.1f}" '
                f'width="{bar:.1f}" height="{h:.1f}" rx="2"><title>{escape(name)}, '
                f"{escape(month_label(month))}: {escape(format_money(value))}</title></rect>"
            )
        if i % every == 0:
            parts.append(
                f'<text class="chart-label" x="{x + bar / 2:.1f}" y="{height - 8}" '
                f'text-anchor="middle">{escape(month.strftime("%b %y"))}</text>'
            )
    parts.append("</svg>")
    return Markup("".join(parts))


def sparkline(values: list[int], *, width: int = 110, height: int = 26) -> Markup:
    """A tiny trend line for a table row."""
    if len(values) < 2 or not any(values):
        return Markup("")
    lo, hi = min(values), max(values)
    if lo == hi:
        hi = lo + 1
    step = (width - 4) / (len(values) - 1)
    points = " ".join(
        f"{2 + i * step:.1f},{2 + (hi - v) * (height - 4) / (hi - lo):.1f}"
        for i, v in enumerate(values)
    )
    return Markup(
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'aria-hidden="true"><polyline class="spark-line" points="{points}"/></svg>'
    )


def line_chart(
    points: list[tuple[date, int]], *, width: int = 720, height: int = 220, label: str = "Chart"
) -> Markup:
    """Line chart of (date, cents). Returns empty markup with fewer than two points."""
    if len(points) < 2:
        return Markup("")
    left, right, top, bottom = 64, 16, 12, 28
    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    if lo == hi:
        lo, hi = lo - 100, hi + 100
    step = (width - left - right) / (len(points) - 1)

    def y(value: int) -> float:
        return top + (hi - value) * (height - top - bottom) / (hi - lo)

    def hline(css: str, at: float) -> str:
        return f'<line class="{css}" x1="{left}" x2="{width - right}" y1="{at:.1f}" y2="{at:.1f}"/>'

    def text(x: float, at: float, content: str, anchor: str = "start") -> str:
        return (
            f'<text class="chart-label" x="{x:.1f}" y="{at:.1f}" text-anchor="{anchor}">'
            f"{escape(content)}</text>"
        )

    coords = [(left + i * step, y(v)) for i, (_, v) in enumerate(points)]
    line = " ".join(
        f"{'M' if i == 0 else 'L'}{cx:.1f},{cy:.1f}" for i, (cx, cy) in enumerate(coords)
    )
    base = height - bottom
    area = f"{line} L{coords[-1][0]:.1f},{base} L{coords[0][0]:.1f},{base} Z"

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(label)}">'
        '<defs><linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" class="chart-fill-a"/><stop offset="1" class="chart-fill-b"/>'
        "</linearGradient></defs>"
    ]
    for value in (lo, (lo + hi) // 2, hi):
        parts.append(hline("chart-grid", y(value)))
        parts.append(text(left - 8, y(value) + 4, compact_money(value), "end"))
    if lo < 0 < hi:
        parts.append(hline("chart-zero", y(0)))
    parts.append(
        f'<path class="chart-area" fill="url(#chart-fill)" d="{area}"/>'
        f'<path class="chart-line" d="{line}"/>'
    )
    for (cx, cy), (day, value) in zip(coords, points, strict=True):
        parts.append(
            f'<circle class="chart-dot" cx="{cx:.1f}" cy="{cy:.1f}" r="3">'
            f"<title>{escape(day.isoformat())}: {escape(format_money(value))}</title></circle>"
        )
    parts.append(text(left, height - 8, month_label(points[0][0])))
    parts.append(text(width - right, height - 8, month_label(points[-1][0]), "end"))
    parts.append("</svg>")
    return Markup("".join(parts))
