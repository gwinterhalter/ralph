"""Fleet cost forecasting (Fleet Analytics spec §2).

Projects spend-to-completion across the fleet from the captured per-Run cost corpus
(``learning_records``) and the live open-work counts: per project, ``mean cost per Run ×
open_work_count`` is the projected remaining spend (D1: per-Run mean is the v1 basis), and the
fleet total adds it to what has already been spent. ``confidence`` scales with sample size so a
project with one Run is flagged low-confidence.

Pure: no I/O, no DB, no wall-clock — operates only on supplied rows + counts (the ``__main__`` /
control-panel wiring reads ``Registry.read_learning_records`` + ``open_work_counts_for``). Money is
exact ``Decimal`` (NFR-007).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

#: Runs needed before a project's forecast is "full" confidence (the FR-050/051 consistency floor).
_CONFIDENCE_RUNS = 3


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    return None


@dataclass(frozen=True)
class ProjectForecast:
    """One project's spend-to-completion projection."""

    project_id: str
    runs_with_cost: int
    total_spent_usd: Decimal
    mean_cost_per_run_usd: Decimal
    open_work_count: int
    projected_remaining_usd: Decimal
    projected_total_usd: Decimal
    confidence: float


@dataclass(frozen=True)
class FleetForecast:
    """The fleet-wide projection: per-project rows + the rolled-up totals."""

    projects: tuple[ProjectForecast, ...]
    fleet_spent_usd: Decimal
    fleet_projected_remaining_usd: Decimal
    fleet_projected_total_usd: Decimal


def forecast_fleet(
    learning_rows: Sequence[Mapping[str, object]],
    open_work_counts: Mapping[str, int],
) -> FleetForecast:
    """Project per-project + fleet spend-to-completion (Fleet Analytics §2).

    ``learning_rows`` are ``learning_records`` rows (``project_slug`` + ``cost_usd``);
    ``open_work_counts`` maps ``project_id -> open work items``. A project with open work but no cost
    history forecasts ``0`` remaining at confidence ``0`` (flagged — cannot project without a basis).
    Projects are sorted by projected remaining spend, descending.
    """
    costs_by_project: dict[str, list[Decimal]] = defaultdict(list)
    for row in learning_rows:
        project = str(row.get("project_slug") or row.get("project_id") or "")
        if not project:
            continue
        cost = _as_decimal(row.get("cost_usd"))
        if cost is not None:
            costs_by_project[project].append(cost)

    project_ids = set(costs_by_project) | set(open_work_counts)
    forecasts: list[ProjectForecast] = []
    fleet_spent = Decimal("0")
    fleet_remaining = Decimal("0")
    for project_id in project_ids:
        costs = costs_by_project.get(project_id, [])
        total_spent = sum(costs, Decimal("0"))
        runs = len(costs)
        mean = (total_spent / runs) if runs else Decimal("0")
        open_count = int(open_work_counts.get(project_id, 0))
        projected_remaining = mean * open_count
        forecasts.append(
            ProjectForecast(
                project_id=project_id,
                runs_with_cost=runs,
                total_spent_usd=total_spent,
                mean_cost_per_run_usd=mean,
                open_work_count=open_count,
                projected_remaining_usd=projected_remaining,
                projected_total_usd=total_spent + projected_remaining,
                confidence=min(1.0, runs / _CONFIDENCE_RUNS),
            )
        )
        fleet_spent += total_spent
        fleet_remaining += projected_remaining

    forecasts.sort(key=lambda f: f.projected_remaining_usd, reverse=True)
    return FleetForecast(
        projects=tuple(forecasts),
        fleet_spent_usd=fleet_spent,
        fleet_projected_remaining_usd=fleet_remaining,
        fleet_projected_total_usd=fleet_spent + fleet_remaining,
    )


def render_forecast(forecast: FleetForecast, *, ceiling_usd: Decimal | None = None) -> str:
    """Render the forecast pane (pure). Adds a runway-vs-ceiling line when a ceiling is supplied."""
    lines = [
        f"cost forecast: fleet spent ${forecast.fleet_spent_usd} + projected remaining "
        f"${forecast.fleet_projected_remaining_usd} = projected total "
        f"${forecast.fleet_projected_total_usd}"
    ]
    if ceiling_usd is not None:
        over = forecast.fleet_projected_total_usd > ceiling_usd
        lines.append(
            f"  ceiling ${ceiling_usd} — projected total "
            f"{'OVER' if over else 'within'} budget"
        )
    if not forecast.projects:
        lines.append("  (no projects with cost history or open work)")
    for project in forecast.projects:
        lines.append(
            f"  {project.project_id}: spent ${project.total_spent_usd} + "
            f"~${project.projected_remaining_usd} for {project.open_work_count} open "
            f"(mean ${project.mean_cost_per_run_usd}/run, confidence {project.confidence:.0%})"
        )
    return "\n".join(lines)


def forecast_breaches(forecast: FleetForecast, ceiling_usd: Decimal) -> bool:
    """True iff the fleet PROJECTED total exceeds the ceiling (the warn-only guard predicate)."""
    return forecast.fleet_projected_total_usd > ceiling_usd


__all__ = [
    "ProjectForecast",
    "FleetForecast",
    "forecast_fleet",
    "render_forecast",
    "forecast_breaches",
]
