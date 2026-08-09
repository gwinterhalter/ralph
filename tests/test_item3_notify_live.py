"""Item 3 — gate-escalation delivery, LIVE SMTP checkpoint (real gmail send).

Verifies the supervisor's notification path against the operator's REAL gmail config — the
same ``F_GMAIL_SMTP_*`` app-password the RL orchestrator's ``lib/notify.sh`` already uses
(review F-2), so no redundant setup. Two tests:

  * config resolution (cheap) — ``SmtpConfig.from_env()`` resolves a working config from
    ``F_GMAIL_SMTP_*`` when present; SKIPPED (not failed) when the env is absent. Sends nothing.
  * real send (opt-in) — actually delivers one notification e-mail through gmail; gated behind
    ``OL_SUPERVISOR_NOTIFY_LIVE=1`` so the suite never e-mails the operator on an ordinary run.

The app-password is read from the environment by ``SmtpConfig`` and is never logged here.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from supervisor.attention import (
    ESCALATION_KIND_SAFETY_GATE,
    Escalation,
    NotificationBatch,
    NotificationPlan,
    UrgencyTier,
)
from supervisor.notifications import (
    SmtpConfig,
    SmtpNotificationPort,
    build_notification_port,
)

pytestmark = pytest.mark.integration

_HAVE_GMAIL = bool(os.environ.get("F_GMAIL_SMTP_USER") and os.environ.get("F_GMAIL_SMTP_TO"))
_OPT_IN = os.environ.get("OL_SUPERVISOR_NOTIFY_LIVE") == "1"


def _plan() -> NotificationPlan:
    esc = Escalation(
        project_id="oltest_notify",
        gate_id="oltest_notify-gate",
        kind=ESCALATION_KIND_SAFETY_GATE,
        reversible=False,
        suggested_option=None,
        confidence=1.0,
        raised_at=datetime.now(UTC),
    )
    return NotificationPlan(
        batches=(NotificationBatch(tier=UrgencyTier.TOP, escalations=(esc,)),), deferred=()
    )


@pytest.mark.skipif(
    not _HAVE_GMAIL,
    reason="requires F_GMAIL_SMTP_USER + F_GMAIL_SMTP_TO (the proven RL gmail config).",
)
def test_item3_from_env_resolves_real_gmail_config() -> None:
    """The supervisor builds a real SMTP port from the existing F_GMAIL_SMTP_* config — proving
    the F-2 reuse — WITHOUT sending anything."""
    cfg = SmtpConfig.from_env()
    assert cfg is not None
    assert cfg.sender and cfg.recipients
    assert isinstance(build_notification_port(), SmtpNotificationPort)


@pytest.mark.skipif(
    not (_HAVE_GMAIL and _OPT_IN),
    reason="real gmail send is opt-in: set OL_SUPERVISOR_NOTIFY_LIVE=1 (+ F_GMAIL_SMTP_*).",
)
def test_item3_real_gmail_send() -> None:
    """End-to-end: deliver one notification e-mail through the operator's real gmail SMTP."""
    cfg = SmtpConfig.from_env()
    assert cfg is not None
    sent = SmtpNotificationPort(cfg).deliver(_plan())  # real STARTTLS send
    assert sent == 1  # one batch delivered
