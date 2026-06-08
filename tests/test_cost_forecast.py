"""Fleet cost forecasting (supervisor.cost_forecast)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from supervisor.cost_forecast import (
    forecast_breaches,
    forecast_fleet,
    render_forecast,
)

pytestmark = pytest.mark.unit


def _row(project: str, cost: str | None) -> dict[str, object]:
    return {"project_slug": project, "cost_usd": None if cost is None else Decimal(cost)}


def test_forecast_projects_remaining_and_fleet_totals() -> None:
    rows = [
        _row("p1", "2.00"),
        _row("p1", "4.00"),  # mean 3.00
        _row("p2", "1.00"),  # mean 1.00
    ]
    open_counts = {"p1": 5, "p2": 2}
    fc = forecast_fleet(rows, open_counts)

    by = {p.project_id: p for p in fc.projects}
    assert by["p1"].mean_cost_per_run_usd == Decimal("3.00")
    assert by["p1"].projected_remaining_usd == Decimal("15.00")  # 3 * 5
    assert by["p1"].projected_total_usd == Decimal("21.00")  # 6 spent + 15
    assert by["p2"].projected_remaining_usd == Decimal("2.00")  # 1 * 2
    assert fc.fleet_spent_usd == Decimal("7.00")  # 6 + 1
    assert fc.fleet_projected_remaining_usd == Decimal("17.00")  # 15 + 2
    assert fc.fleet_projected_total_usd == Decimal("24.00")
    # sorted by projected remaining desc → p1 first
    assert fc.projects[0].project_id == "p1"


def test_forecast_no_cost_history_is_zero_confidence() -> None:
    # A project with open work but no captured cost → cannot project (0 remaining, 0 confidence).
    fc = forecast_fleet([], {"newproj": 10})
    p = fc.projects[0]
    assert p.project_id == "newproj"
    assert p.projected_remaining_usd == Decimal("0")
    assert p.confidence == 0.0


def test_confidence_scales_with_runs() -> None:
    one = forecast_fleet([_row("p", "1.0")], {"p": 1}).projects[0]
    three = forecast_fleet([_row("p", "1.0")] * 3, {"p": 1}).projects[0]
    assert one.confidence == pytest.approx(1 / 3)
    assert three.confidence == 1.0


def test_render_and_breaches() -> None:
    fc = forecast_fleet([_row("p", "10.0")], {"p": 2})  # total 10 + 20 = 30
    assert fc.fleet_projected_total_usd == Decimal("30.0")
    assert forecast_breaches(fc, Decimal("25")) is True
    assert forecast_breaches(fc, Decimal("100")) is False
    out = render_forecast(fc, ceiling_usd=Decimal("25"))
    assert "projected total $30.0" in out
    assert "OVER budget" in out


def test_render_empty() -> None:
    out = render_forecast(forecast_fleet([], {}))
    assert "no projects" in out
