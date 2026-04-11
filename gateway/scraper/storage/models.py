"""
Data models for the scraper's local SQLite database.

These are plain dataclasses — no ORM. The db module handles all SQL directly
(matching the main server's pattern of raw sqlite3 access).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


@dataclass
class RawPost:
    id: str                         # "twitter:{tweet_id}" or "truthsocial:{status_id}"
    platform: str                   # "twitter" | "truthsocial"
    author_handle: str
    author_display_name: str
    author_followers: int
    author_verified: bool
    content: str
    posted_at: datetime
    scraped_at: datetime
    likes: int
    retweets_or_boosts: int
    replies: int
    keyword_matched: str
    transmitted: bool = False
    transmission_attempts: int = 0
    last_transmission_attempt: Optional[datetime] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["posted_at"] = self.posted_at.isoformat()
        d["scraped_at"] = self.scraped_at.isoformat()
        d["last_transmission_attempt"] = (
            self.last_transmission_attempt.isoformat()
            if self.last_transmission_attempt else None
        )
        return d

    @classmethod
    def from_row(cls, row: dict) -> RawPost:
        return cls(
            id=row["id"],
            platform=row["platform"],
            author_handle=row["author_handle"],
            author_display_name=row["author_display_name"],
            author_followers=row["author_followers"],
            author_verified=bool(row["author_verified"]),
            content=row["content"],
            posted_at=datetime.fromisoformat(row["posted_at"]),
            scraped_at=datetime.fromisoformat(row["scraped_at"]),
            likes=row["likes"],
            retweets_or_boosts=row["retweets_or_boosts"],
            replies=row["replies"],
            keyword_matched=row["keyword_matched"],
            transmitted=bool(row["transmitted"]),
            transmission_attempts=row["transmission_attempts"],
            last_transmission_attempt=(
                datetime.fromisoformat(row["last_transmission_attempt"])
                if row["last_transmission_attempt"] else None
            ),
        )


@dataclass
class ScraperRun:
    id: Optional[int]
    platform: str
    keyword: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    posts_found: int = 0
    posts_new: int = 0
    posts_transmitted: int = 0
    error: Optional[str] = None
    duration_seconds: Optional[float] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat()
        d["completed_at"] = self.completed_at.isoformat() if self.completed_at else None
        return d

    @classmethod
    def from_row(cls, row: dict) -> ScraperRun:
        return cls(
            id=row["id"],
            platform=row["platform"],
            keyword=row["keyword"],
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"] else None
            ),
            posts_found=row["posts_found"],
            posts_new=row["posts_new"],
            posts_transmitted=row["posts_transmitted"],
            error=row["error"],
            duration_seconds=row["duration_seconds"],
        )


@dataclass
class ScraperSession:
    id: Optional[int]
    platform: str
    session_path: str
    created_at: datetime
    last_used_at: Optional[datetime] = None
    valid: bool = True
    notes: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["last_used_at"] = self.last_used_at.isoformat() if self.last_used_at else None
        return d

    @classmethod
    def from_row(cls, row: dict) -> ScraperSession:
        return cls(
            id=row["id"],
            platform=row["platform"],
            session_path=row["session_path"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_used_at=(
                datetime.fromisoformat(row["last_used_at"])
                if row["last_used_at"] else None
            ),
            valid=bool(row["valid"]),
            notes=row["notes"],
        )
