"""SMTP fake — captures every outbound email for assertions.

Any code that goes through ``email_system.service.EmailService.send`` or
``send_template`` ends up in ``MockMailer.outbox``. Tests assert on
subject / body / recipient to check behaviour without shelling out to
an MTA.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pytest


@dataclass
class CapturedEmail:
    to: str
    subject: str
    html: str = ""
    text: str = ""
    reply_to: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    template: Optional[str] = None
    context: dict[str, Any] = field(default_factory=dict)


class MockMailer:
    def __init__(self):
        self.outbox: list[CapturedEmail] = []

    async def send(self, *, to: str, subject: str, html: str = "",
                   text: str = "", reply_to: Optional[str] = None,
                   tags: Optional[list[str]] = None, **_: Any) -> None:
        self.outbox.append(CapturedEmail(
            to=to, subject=subject, html=html, text=text,
            reply_to=reply_to, tags=list(tags or []),
        ))

    async def send_template(self, *, to: str, template: str,
                            context: Optional[dict] = None,
                            reply_to: Optional[str] = None,
                            tags: Optional[list[str]] = None,
                            **_: Any) -> None:
        ctx = dict(context or {})
        self.outbox.append(CapturedEmail(
            to=to, subject=f"[tpl:{template}]",
            html="", text="", reply_to=reply_to,
            tags=list(tags or []),
            template=template, context=ctx,
        ))

    # Helpers that tests love to have ─────────────────────────────
    def by_template(self, name: str) -> list[CapturedEmail]:
        return [e for e in self.outbox if e.template == name]

    def to_recipient(self, address: str) -> list[CapturedEmail]:
        return [e for e in self.outbox if e.to == address]

    def clear(self) -> None:
        self.outbox.clear()


@pytest.fixture
def mock_mailer(monkeypatch):
    """Replace the singleton EmailService with a MockMailer."""
    mailer = MockMailer()
    try:
        from email_system import service as _svc
        monkeypatch.setattr(_svc, "get_email_service", lambda: mailer)
    except Exception:
        pass
    try:
        # Some code paths go through jobs.email_jobs.enqueue_email.
        from jobs import email_jobs as _ej

        async def _fake_enqueue(**kwargs):
            await mailer.send_template(
                to=kwargs.get("to") or "",
                template=kwargs.get("template") or "",
                context=kwargs.get("context") or {},
                tags=kwargs.get("tags"),
            )

        monkeypatch.setattr(_ej, "enqueue_email", _fake_enqueue, raising=False)
    except Exception:
        pass
    return mailer
