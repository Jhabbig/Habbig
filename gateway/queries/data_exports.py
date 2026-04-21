"""Queries extracted from gateway/db.py — data_exports domain.

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


def create_data_export_request(user_id: int) -> int:
    """Insert a pending export row; returns its id.

    Caller is responsible for rate-limiting (1/24h/user) before calling
    this — the DB has no such constraint so we can backfill retries without
    tripping a unique index.
    """
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO data_export_requests "
            "(user_id, requested_at, status) VALUES (?, ?, 'pending')",
            (user_id, int(time.time())),
        )
        return cur.lastrowid


def get_data_export_request(export_id: int):
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM data_export_requests WHERE id = ?",
            (export_id,),
        ).fetchone()


def list_user_data_exports(user_id: int, limit: int = 20):
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM data_export_requests "
            "WHERE user_id = ? "
            "ORDER BY requested_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def last_user_data_export_ts(user_id: int):
    """Most recent requested_at for rate-limit checking. None if never."""
    with db.conn() as c:
        row = c.execute(
            "SELECT requested_at FROM data_export_requests "
            "WHERE user_id = ? ORDER BY requested_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return int(row["requested_at"]) if row else None


def update_data_export_request(
    export_id: int,
    *,
    status: Optional[str] = None,
    completed_at: Optional[int] = None,
    download_url: Optional[str] = None,
    expires_at: Optional[int] = None,
    file_size_bytes: Optional[int] = None,
    file_path: Optional[str] = None,
    error: Optional[str] = None,
) -> bool:
    fields = []
    params = []
    if status is not None:
        fields.append("status = ?"); params.append(status)
    if completed_at is not None:
        fields.append("completed_at = ?"); params.append(completed_at)
    if download_url is not None:
        fields.append("download_url = ?"); params.append(download_url)
    if expires_at is not None:
        fields.append("expires_at = ?"); params.append(expires_at)
    if file_size_bytes is not None:
        fields.append("file_size_bytes = ?"); params.append(file_size_bytes)
    if file_path is not None:
        fields.append("file_path = ?"); params.append(file_path)
    if error is not None:
        fields.append("error = ?"); params.append(error)
    if not fields:
        return False
    params.append(export_id)
    with db.conn() as c:
        cur = c.execute(
            f"UPDATE data_export_requests SET {', '.join(fields)} WHERE id = ?",
            tuple(params),
        )
        return cur.rowcount > 0


__all__ = [
    'create_data_export_request',
    'get_data_export_request',
    'list_user_data_exports',
    'last_user_data_export_ts',
    'update_data_export_request',
]
