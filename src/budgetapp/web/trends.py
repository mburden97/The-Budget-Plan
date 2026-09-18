"""Spending trends by category or budget line over recent complete months, and net worth
projected forward at the current plan."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from flask import Blueprint, flash, render_template, request

from budgetapp import projection, trends
from budgetapp.charts import SERIES_COLORS, line_chart, sparkline, stacked_bars
from budgetapp.dates import add_months, month_start
from budgetapp.money import parse_percent
from budgetapp.web import current_store

bp = Blueprint("trends", __name__, url_prefix="/trends")

RANGES = (6, 12, 24)
YEARS = (1, 2, 5, 10)
MAX_GROWTH = Decimal("0.30")
TOP = SERIES_COLORS - 1  # the last color is "Other"


@bp.get("/")
def index():
    args = request.args
    months = int(args["months"]) if args.get("months") in {str(r) for r in RANGES} else 12
    by = args.get("by") if args.get("by") in trends.GROUPINGS else "category"
    options = _projection_options(args)
    today = date.today()
    last_full_month = add_months(month_start(today), -1)
    with current_store().read() as conn:
        result = trends.spending_trends(conn, end=last_full_month, months=months, by=by)
        projected = projection.project(
            conn, today=today, months=options["years"] * 12, growth=options["growth"],
            windfalls=options["windfalls"], redirect_payoffs=options["payoffs"],
        )

    top, rest = result.series[:TOP], result.series[TOP:]
    chart_series = [(s.label, s.values) for s in top]
    if rest:
        chart_series.append(("Other", [sum(s.values[i] for s in rest) for i in range(months)]))
    totals = result.totals
    return render_template(
        "trends.html",
        result=result,
        chart=stacked_bars(result.months, chart_series, label="Monthly spending"),
        legend=[(name, i) for i, (name, _values) in enumerate(chart_series)],
        rows=[
            (s, sparkline(s.values), i if i < TOP else None) for i, s in enumerate(result.series)
        ],
        total_spark=sparkline(totals),
        totals=totals,
        avg_spent=round(sum(totals) / months),
        avg_income=round(sum(result.income) / months),
        months=months,
        by=by,
        ranges=RANGES,
        groupings=trends.GROUPINGS,
        projected=projected,
        projection_chart=line_chart(
            [(p.when, p.net_cents) for p in projected.points], label="Projected net worth"
        ),
        milestones=_milestones(projected),
        options=options,
        year_options=YEARS,
    )


def _projection_options(args) -> dict:
    """Projection settings from the query string; defaults until the form is submitted."""
    years = int(args["years"]) if args.get("years") in {str(y) for y in YEARS} else 2
    submitted = args.get("p") == "1"
    growth_input = (args.get("growth") or "").strip() or "0"
    try:
        growth = parse_percent(growth_input, label="Investment growth")
        if growth > MAX_GROWTH:
            raise ValueError("Investment growth: use 30% a year or less.")
    except ValueError as exc:
        flash(str(exc), "error")
        growth, growth_input = Decimal(0), "0"
    windfalls = args.get("windfalls") == "1" if submitted else True
    payoffs = args.get("payoffs") == "1" if submitted else True
    keep: dict = {}  # carried along when switching the spending range or grouping
    if submitted:
        keep = {"years": years, "growth": growth_input, "p": 1}
        keep |= {"windfalls": 1} if windfalls else {}
        keep |= {"payoffs": 1} if payoffs else {}
    return {"years": years, "growth": growth, "growth_input": growth_input,
            "windfalls": windfalls, "payoffs": payoffs, "keep": keep}


def _milestones(projected: projection.Projection) -> list[projection.Point]:
    last = len(projected.points) - 1
    marks = sorted({m for m in (0, 6, 12, 24, 60, 120) if m <= last} | {last})
    return [projected.points[m] for m in marks]
