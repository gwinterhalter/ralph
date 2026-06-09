"""T3#6 — opt-in hard emergency spend ceiling (supervisor.spend_backstop)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from supervisor.safety_gates import ESCALATION_TIER_TOP, PAUSED_SAFETY
from supervisor.spend_backstop import EmergencySpendConfig, evaluate_spend_backstop

pytestmark = pytest.mark.unit


def test_disabled_by_default_never_fires() -> None:
    # Default config (ceiling None) is OFF — preserves the breaker no-cap design.
    cfg = EmergencySpendConfig()
    assert cfg.ceiling_usd is None
    assert evaluate_spend_backstop(Decimal("999999"), cfg, project_id="p") is None


def test_below_ceiling_no_trip() -> None:
    cfg = EmergencySpendConfig(ceiling_usd=Decimal("400"))
    assert evaluate_spend_backstop(Decimal("399.9999"), cfg, project_id="p") is None


def test_at_ceiling_trips_kill() -> None:
    cfg = EmergencySpendConfig(ceiling_usd=Decimal("400"))
    esc = evaluate_spend_backstop(Decimal("400"), cfg, project_id="p")
    assert esc is not None
    assert esc.killed is True
    assert esc.tier == ESCALATION_TIER_TOP
    assert esc.lifecycle_state == PAUSED_SAFETY
    assert esc.project_id == "p"


def test_above_ceiling_reason_carries_both_figures() -> None:
    cfg = EmergencySpendConfig(ceiling_usd=Decimal("400"))
    esc = evaluate_spend_backstop(Decimal("412.50"), cfg, project_id="ol_build")
    assert esc is not None
    assert "412.50" in esc.reason and "400" in esc.reason
    assert "kill" in esc.reason.lower()


def test_decimal_exactness_no_float() -> None:
    # Exact-Decimal arithmetic at the >= boundary: Decimal("0.1")+Decimal("0.2") is
    # exactly Decimal("0.3") (a float sum would be 0.30000000000000004), so it lands
    # ON a 0.30 ceiling and trips; a value just under does not.
    cfg = EmergencySpendConfig(ceiling_usd=Decimal("0.30"))
    assert evaluate_spend_backstop(Decimal("0.1") + Decimal("0.2"), cfg, project_id="p") is not None
    assert evaluate_spend_backstop(Decimal("0.29"), cfg, project_id="p") is None
