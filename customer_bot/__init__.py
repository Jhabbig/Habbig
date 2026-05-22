"""Customer-finding bot for narve.ai.

Polls Reddit, Hacker News, and Polymarket for users discussing topics that
match a narve.ai dashboard, scores them, drafts personalised outreach, and
writes them to the shared gateway SQLite database for human review and
manual send via the Admin → Leads tab.

Nothing in this package ever posts, replies, or DMs autonomously. Every
outbound action is a click in the admin UI.
"""

from customer_bot.runner import LeadsPoller

__all__ = ["LeadsPoller"]
