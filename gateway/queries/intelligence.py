"""Queries extracted from gateway/db.py — intelligence domain.

Moved out of db.py to keep the connection-pooling/schema module small.
Re-exported back onto db.py at import time, so every existing
``import db; db.<name>`` call site keeps working unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
from typing import Optional

import db


def create_intelligence_conversation(user_id: int, title: Optional[str] = None) -> int:
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO intelligence_conversations (user_id, title, message_count, created_at, updated_at) "
            "VALUES (?, ?, 0, ?, ?)",
            (user_id, title, now, now),
        )
        return cur.lastrowid


def list_intelligence_conversations(user_id: int, limit: int = 50) -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM intelligence_conversations WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def get_intelligence_conversation(conv_id: int, user_id: int) -> Optional[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM intelligence_conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()


def list_intelligence_messages(conv_id: int, limit: int = 200) -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM intelligence_messages WHERE conversation_id = ? ORDER BY created_at ASC LIMIT ?",
            (conv_id, limit),
        ).fetchall()


def append_intelligence_message(
    conv_id: int,
    role: str,
    content: str,
    context_used: Optional[dict] = None,
    tokens_used: Optional[int] = None,
) -> int:
    import json as _json
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO intelligence_messages (conversation_id, role, content, context_used, tokens_used, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conv_id, role, content, _json.dumps(context_used) if context_used else None, tokens_used, now),
        )
        title_candidate = content[:80] if role == "user" else None
        c.execute(
            "UPDATE intelligence_conversations SET message_count = message_count + 1, updated_at = ?, "
            "title = COALESCE(title, ?) WHERE id = ?",
            (now, title_candidate, conv_id),
        )
        return cur.lastrowid


def delete_intelligence_conversation(conv_id: int, user_id: int) -> bool:
    with db.conn() as c:
        cur = c.execute(
            "DELETE FROM intelligence_conversations WHERE id = ? AND user_id = ?",
            (conv_id, user_id),
        )
        return cur.rowcount > 0


def count_intelligence_messages_today(user_id: int) -> int:
    day_cut = int(time.time()) - 86400
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM intelligence_messages im "
            "INNER JOIN intelligence_conversations ic ON im.conversation_id = ic.id "
            "WHERE ic.user_id = ? AND im.role = 'user' AND im.created_at >= ?",
            (user_id, day_cut),
        ).fetchone()
    return row[0] if row else 0


__all__ = [
    'create_intelligence_conversation',
    'list_intelligence_conversations',
    'get_intelligence_conversation',
    'list_intelligence_messages',
    'append_intelligence_message',
    'delete_intelligence_conversation',
    'count_intelligence_messages_today',
]
