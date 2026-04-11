"""Unified email subsystem for narve.ai.

Public surface:

    from email_system import EmailService, TEMPLATES, UnsubscribeManager

Never send email inline from a route handler — always go through
`jobs.email_jobs.enqueue_email(...)`, which delegates here from the worker.

Named `email_system` (not `email`) because `email` is a Python stdlib
package and shadowing it breaks mime handling.
"""

from email_system.service import EmailService  # noqa: F401
from email_system.unsubscribe import UnsubscribeManager  # noqa: F401

TEMPLATES = [
    "token_delivery",
    "welcome",
    "payment_failed",
    "subscription_cancelled",
    "password_reset",
    "account_deletion_confirmation",
    "account_deleted",
    "weekly_digest",
    "market_resolved",
    "unsubscribe_confirmation",
]
