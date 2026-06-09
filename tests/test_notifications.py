"""T2#4 — notification delivery port (supervisor.notifications)."""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from supervisor.attention import (
    Escalation,
    NotificationBatch,
    NotificationPlan,
    UrgencyTier,
)
from supervisor.notifications import (
    NullNotificationPort,
    SmtpConfig,
    SmtpNotificationPort,
    build_notification_port,
    render_batch,
)

pytestmark = pytest.mark.unit

_T0 = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)


def _esc(pid: str, *, tier_kind: str = "safety_gate", reversible: bool = False) -> Escalation:
    return Escalation(
        project_id=pid,
        gate_id=f"{pid}-gate",
        kind=tier_kind,
        reversible=reversible,
        suggested_option="A",
        confidence=0.91,
        raised_at=_T0,
    )


def _plan(*batches: NotificationBatch) -> NotificationPlan:
    return NotificationPlan(batches=tuple(batches), deferred=())


class _FakeSmtp:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.tls = False
        self.logged_in: tuple[str, str] | None = None

    def starttls(self) -> object:
        self.tls = True
        return None

    def login(self, user: str, password: str) -> object:
        self.logged_in = (user, password)
        return None

    def send_message(self, msg: EmailMessage) -> object:
        self.sent.append(msg)
        return {}

    def __enter__(self) -> "_FakeSmtp":
        return self

    def __exit__(self, *exc: object) -> object:
        return None


_CFG = SmtpConfig(
    host="smtp.example.com",
    port=587,
    sender="supervisor@example.com",
    recipients=("greg@example.com",),
    username="u",
    password="p",
)


def test_render_batch_lists_escalations() -> None:
    batch = NotificationBatch(tier=UrgencyTier.TOP, escalations=(_esc("p1"),))
    subject, body = render_batch(batch)
    assert "TOP" in subject and "1 escalation" in subject
    assert "p1 / p1-gate" in body
    assert "suggested=A" in body
    assert "confidence=0.91" in body


def test_smtp_port_sends_one_message_per_batch() -> None:
    fake = _FakeSmtp()
    port = SmtpNotificationPort(_CFG, smtp_factory=lambda: fake)
    plan = _plan(
        NotificationBatch(tier=UrgencyTier.TOP, escalations=(_esc("p1"),)),
        NotificationBatch(tier=UrgencyTier.ROUTINE, escalations=(_esc("p2"), _esc("p3"))),
    )
    sent = port.deliver(plan)
    assert sent == 2
    assert len(fake.sent) == 2
    assert fake.tls is True
    assert fake.logged_in == ("u", "p")
    assert fake.sent[0]["To"] == "greg@example.com"
    assert fake.sent[0]["From"] == "supervisor@example.com"


def test_smtp_port_noop_on_empty_plan() -> None:
    fake = _FakeSmtp()
    port = SmtpNotificationPort(_CFG, smtp_factory=lambda: fake)
    assert port.deliver(_plan()) == 0  # no batches (quiet-hours deferral / empty fleet)
    assert fake.sent == []


def test_smtp_port_noop_without_recipients() -> None:
    fake = _FakeSmtp()
    cfg = SmtpConfig(host="h", port=587, sender="s@x", recipients=())
    port = SmtpNotificationPort(cfg, smtp_factory=lambda: fake)
    plan = _plan(NotificationBatch(tier=UrgencyTier.TOP, escalations=(_esc("p1"),)))
    assert port.deliver(plan) == 0
    assert fake.sent == []


def test_smtp_port_skips_tls_and_login_when_unset() -> None:
    fake = _FakeSmtp()
    cfg = SmtpConfig(host="h", port=25, sender="s@x", recipients=("r@x",), use_tls=False)
    port = SmtpNotificationPort(cfg, smtp_factory=lambda: fake)
    port.deliver(_plan(NotificationBatch(tier=UrgencyTier.TOP, escalations=(_esc("p1"),))))
    assert fake.tls is False
    assert fake.logged_in is None


_ALL_SMTP_ENV = (
    "OL_SUPERVISOR_SMTP_HOST",
    "OL_SUPERVISOR_SMTP_FROM",
    "OL_SUPERVISOR_SMTP_TO",
    "OL_SUPERVISOR_SMTP_PORT",
    "OL_SUPERVISOR_SMTP_USER",
    "OL_SUPERVISOR_SMTP_PASSWORD",
    "F_GMAIL_SMTP_USER",
    "F_GMAIL_SMTP_TO",
    "F_GMAIL_SMTP_APP_PASSWORD",
    "F_GMAIL_SMTP_HOST",
    "F_GMAIL_SMTP_PORT",
)


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ALL_SMTP_ENV:
        monkeypatch.delenv(var, raising=False)
    assert SmtpConfig.from_env() is None  # unconfigured (neither OL_* nor F_GMAIL_*)

    monkeypatch.setenv("OL_SUPERVISOR_SMTP_HOST", "smtp.x")
    monkeypatch.setenv("OL_SUPERVISOR_SMTP_FROM", "s@x")
    monkeypatch.setenv("OL_SUPERVISOR_SMTP_TO", "a@x, b@x")
    cfg = SmtpConfig.from_env()
    assert cfg is not None
    assert cfg.recipients == ("a@x", "b@x")


def test_from_env_falls_back_to_f_gmail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review F-2: with no OL_SUPERVISOR_SMTP_* but the proven F_GMAIL_SMTP_* set, the
    supervisor builds a working gmail config (zero redundant setup)."""
    for var in _ALL_SMTP_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("F_GMAIL_SMTP_USER", "rl@gmail.com")
    monkeypatch.setenv("F_GMAIL_SMTP_TO", "greg@example.com")
    monkeypatch.setenv("F_GMAIL_SMTP_APP_PASSWORD", "app-pw")
    cfg = SmtpConfig.from_env()
    assert cfg is not None
    assert cfg.sender == "rl@gmail.com"
    assert cfg.username == "rl@gmail.com"
    assert cfg.password == "app-pw"
    assert cfg.recipients == ("greg@example.com",)
    assert cfg.host == "smtp.gmail.com"  # gmail default
    assert cfg.port == 587  # gmail default


def test_build_notification_port_falls_back_to_null(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ALL_SMTP_ENV:
        monkeypatch.delenv(var, raising=False)
    port = build_notification_port()
    assert isinstance(port, NullNotificationPort)
    assert port.deliver(_plan(NotificationBatch(tier=UrgencyTier.TOP, escalations=(_esc("p1"),)))) == 0
