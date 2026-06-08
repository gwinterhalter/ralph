"""Notification delivery for the Operator-Attention layer (robustness T2#4).

OLB-10 (:mod:`supervisor.attention`) *plans* operator notifications — top-tier
first, routine collapsed into one windowed batch, quiet-hours deferred — but the
actual out-of-band **delivery** was never wired: a planned :class:`NotificationPlan`
was computed and dropped. So a ``gate_human`` escalation or a safety trip reached
no operator who had stepped away. This module is the delivery port: it renders each
planned :class:`NotificationBatch` and sends it over SMTP.

Design: delivery is injectable (``smtp_factory``) so tests assert the rendered
messages without opening a socket, and ``render_batch`` is a pure, golden-assertable
formatter. The port is a **no-op** when unconfigured (no recipients) or when the
plan has no batches — quiet-hours deferral and an empty fleet stay silent. The
production cycle builds the config from ``OL_SUPERVISOR_SMTP_*`` env vars (parallel
to ``OL_SUPERVISOR_DB_URL``); when they are unset, :class:`NullNotificationPort` is
used and nothing is sent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable, Protocol, cast

from supervisor.attention import NotificationBatch, NotificationPlan


@dataclass(frozen=True)
class SmtpConfig:
    """SMTP connection + envelope settings for notification delivery."""

    host: str
    port: int
    sender: str
    recipients: tuple[str, ...]
    username: str | None = None
    password: str | None = None
    use_tls: bool = True

    @classmethod
    def from_env(cls) -> "SmtpConfig | None":
        """Build from env, or None when not configured.

        Prefers explicit ``OL_SUPERVISOR_SMTP_*`` overrides, then falls back to the proven
        inner-loop ``F_GMAIL_SMTP_*`` config the RL orchestrator's ``lib/notify.sh`` already
        uses (review F-2) — so the supervisor delivers via the operator's existing gmail
        app-password with NO redundant setup. The gmail defaults (``smtp.gmail.com`` / ``587`` /
        STARTTLS) apply when only the identity vars are present. Requires at least a sender and a
        non-empty recipient list; returns None otherwise so the caller falls back to the no-op
        port. The app-password value is read from env and never logged.
        """
        def _env(*names: str) -> str | None:
            for name in names:
                value = os.environ.get(name)
                if value:
                    return value
            return None

        sender = _env("OL_SUPERVISOR_SMTP_FROM", "F_GMAIL_SMTP_USER")
        raw_to = _env("OL_SUPERVISOR_SMTP_TO", "F_GMAIL_SMTP_TO") or ""
        recipients = tuple(r.strip() for r in raw_to.split(",") if r.strip())
        if not sender or not recipients:
            return None
        host = _env("OL_SUPERVISOR_SMTP_HOST", "F_GMAIL_SMTP_HOST") or "smtp.gmail.com"
        port = int(_env("OL_SUPERVISOR_SMTP_PORT", "F_GMAIL_SMTP_PORT") or "587")
        return cls(
            host=host,
            port=port,
            sender=sender,
            recipients=recipients,
            # login user defaults to the gmail account (F_GMAIL_SMTP_USER is both sender + login).
            username=_env("OL_SUPERVISOR_SMTP_USER", "F_GMAIL_SMTP_USER"),
            password=_env("OL_SUPERVISOR_SMTP_PASSWORD", "F_GMAIL_SMTP_APP_PASSWORD"),
            use_tls=os.environ.get("OL_SUPERVISOR_SMTP_TLS", "1") != "0",
        )


class SmtpClient(Protocol):
    """The minimal SMTP surface the port drives (``smtplib.SMTP`` satisfies it)."""

    def starttls(self) -> object: ...
    def login(self, user: str, password: str) -> object: ...
    def send_message(self, msg: EmailMessage) -> object: ...
    def __enter__(self) -> "SmtpClient": ...
    def __exit__(self, *exc: object) -> object: ...


class NotificationPort(Protocol):
    """Delivers a planned :class:`NotificationPlan`; returns the batch count sent."""

    def deliver(self, plan: NotificationPlan) -> int: ...


def render_batch(batch: NotificationBatch) -> tuple[str, str]:
    """Render one planned batch to an ``(subject, body)`` pair (pure)."""
    tier = batch.tier.name
    count = len(batch.escalations)
    subject = f"[ol-build supervisor] {tier} — {count} escalation(s) need attention"
    lines = [f"{count} {tier.lower()}-tier escalation(s) require operator attention:", ""]
    for esc in batch.escalations:
        suggested = f" suggested={esc.suggested_option}" if esc.suggested_option else ""
        lines.append(
            f"- {esc.project_id} / {esc.gate_id} [{esc.kind}] "
            f"reversible={esc.reversible} confidence={esc.confidence:.2f}{suggested} "
            f"(raised {esc.raised_at.isoformat()})"
        )
    if batch.window_start is not None and batch.window_end is not None:
        lines.append("")
        lines.append(
            f"batch window: {batch.window_start.isoformat()} .. {batch.window_end.isoformat()}"
        )
    return subject, "\n".join(lines)


class NullNotificationPort:
    """The default port when SMTP is unconfigured — delivers nothing."""

    def deliver(self, plan: NotificationPlan) -> int:
        return 0


@dataclass
class SmtpNotificationPort:
    """Deliver a :class:`NotificationPlan` over SMTP, one message per batch.

    ``smtp_factory`` is injected so a test supplies a fake client; when None, a real
    ``smtplib.SMTP(host, port)`` is opened lazily. No-op when there are no recipients
    or no batches (quiet-hours deferral / empty fleet stay silent).
    """

    config: SmtpConfig
    smtp_factory: Callable[[], SmtpClient] | None = None

    def _client(self) -> SmtpClient:
        if self.smtp_factory is not None:
            return self.smtp_factory()
        import smtplib

        return cast(SmtpClient, smtplib.SMTP(self.config.host, self.config.port))

    def deliver(self, plan: NotificationPlan) -> int:
        if not self.config.recipients or not plan.batches:
            return 0
        sent = 0
        with self._client() as client:
            if self.config.use_tls:
                client.starttls()
            if self.config.username and self.config.password:
                client.login(self.config.username, self.config.password)
            for batch in plan.batches:
                subject, body = render_batch(batch)
                msg = EmailMessage()
                msg["From"] = self.config.sender
                msg["To"] = ", ".join(self.config.recipients)
                msg["Subject"] = subject
                msg.set_content(body)
                client.send_message(msg)
                sent += 1
        return sent


def build_notification_port(
    config: SmtpConfig | None = None,
    *,
    smtp_factory: Callable[[], SmtpClient] | None = None,
) -> NotificationPort:
    """Build the production notification port: SMTP when configured, else no-op.

    With ``config=None`` reads :meth:`SmtpConfig.from_env`; an unconfigured
    environment yields :class:`NullNotificationPort` (nothing sent).
    """
    resolved = config if config is not None else SmtpConfig.from_env()
    if resolved is None:
        return NullNotificationPort()
    return SmtpNotificationPort(resolved, smtp_factory=smtp_factory)


__all__ = [
    "SmtpConfig",
    "SmtpClient",
    "NotificationPort",
    "NullNotificationPort",
    "SmtpNotificationPort",
    "render_batch",
    "build_notification_port",
]
