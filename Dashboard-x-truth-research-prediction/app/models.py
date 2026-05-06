from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Column, Field, SQLModel, Text


class PlatformEnum(str, Enum):
    twitter = "twitter"
    truthsocial = "truthsocial"


class CategoryEnum(str, Enum):
    politics = "politics"
    sports = "sports"
    crypto = "crypto"
    geopolitics = "geopolitics"
    other = "other"


class User(SQLModel, table=True):
    __tablename__ = "user"
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    email: str = Field(default="", index=True)
    password_hash: str = ""
    twitter_bearer_token: str = ""
    truthsocial_username: str = ""
    truthsocial_password: str = ""
    truthsocial_access_token: str = ""
    telegram_bot_token: str = ""  # Fernet-encrypted (set via /profile/update)
    telegram_chat_id: str = ""  # plain — chat IDs aren't secret on their own
    telegram_alerts_enabled: bool = False
    preferred_platform: str = Field(default="polymarket")  # "polymarket" or "kalshi"
    preferred_theme: str = Field(default="dark")  # "dark" or "light"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RawPost(SQLModel, table=True):
    __tablename__ = "raw_post"
    id: str = Field(primary_key=True)
    platform: str = Field(index=True)
    author_handle: str = Field(index=True)
    author_display_name: str = ""
    follower_count: int = 0
    verified: bool = False
    content: str = Field(default="", sa_column=Column(Text))
    posted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    engagement_json: str = Field(default="{}", sa_column=Column(Text))

    @property
    def engagement(self) -> dict:
        return json.loads(self.engagement_json)

    @engagement.setter
    def engagement(self, value: dict) -> None:
        self.engagement_json = json.dumps(value)


class Prediction(SQLModel, table=True):
    __tablename__ = "prediction"
    id: Optional[int] = Field(default=None, primary_key=True)
    raw_post_id: str = Field(foreign_key="raw_post.id", index=True)
    market_slug: Optional[str] = Field(default=None, index=True)
    market_question: Optional[str] = None
    market_close_time: Optional[datetime] = None
    hours_remaining_at_prediction: Optional[float] = None
    counts_toward_credibility: bool = False
    category: str = Field(default="other")
    predicted_outcome: str = ""
    predicted_probability: Optional[float] = None
    market_implied_probability: Optional[float] = None
    ev_score: Optional[float] = None
    bet_side: str = Field(default="YES")  # "YES" or "NO" — which side carries the EV
    global_credibility_at_time: float = 0.0
    category_credibility_at_time: Optional[float] = None
    risk_flag: bool = False
    risk_reasons_json: str = Field(default="[]", sa_column=Column(Text))
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved: bool = False
    resolved_correct: Optional[bool] = None

    @property
    def risk_reasons(self) -> list[str]:
        return json.loads(self.risk_reasons_json)

    @risk_reasons.setter
    def risk_reasons(self, value: list[str]) -> None:
        self.risk_reasons_json = json.dumps(value)


class Source(SQLModel, table=True):
    __tablename__ = "source"
    handle: str = Field(primary_key=True)
    platform: str = Field(default="twitter")
    trusted: Optional[bool] = None
    global_credibility: float = 0.0
    category_credibility_json: str = Field(default="{}", sa_column=Column(Text))
    follower_count: int = 0
    verified: bool = False
    engagement_ratio: float = 0.0
    total_predictions: int = 0
    qualifying_predictions: int = 0
    correct_qualifying: int = 0
    categories_predicted_in_json: str = Field(default="[]", sa_column=Column(Text))
    accuracy_unlocked: bool = False
    accuracy_global: Optional[float] = None
    decay_weighted_accuracy: Optional[float] = None
    last_seen: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def category_credibility(self) -> dict:
        return json.loads(self.category_credibility_json)

    @category_credibility.setter
    def category_credibility(self, value: dict) -> None:
        self.category_credibility_json = json.dumps(value)

    @property
    def categories_predicted_in(self) -> list[str]:
        return json.loads(self.categories_predicted_in_json)

    @categories_predicted_in.setter
    def categories_predicted_in(self, value: list[str]) -> None:
        self.categories_predicted_in_json = json.dumps(value)


class SourcePredictionRecord(SQLModel, table=True):
    __tablename__ = "source_prediction_record"
    id: Optional[int] = Field(default=None, primary_key=True)
    handle: str = Field(foreign_key="source.handle", index=True)
    prediction_id: int = Field(foreign_key="prediction.id", index=True)
    market_slug: str = ""
    category: str = ""
    predicted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    hours_remaining: float = 0.0
    resolved_correct: Optional[bool] = None
    decay_weight: float = 1.0
    counted: bool = False


class MarketSnapshot(SQLModel, table=True):
    __tablename__ = "market_snapshot"
    id: Optional[int] = Field(default=None, primary_key=True)
    market_slug: str = Field(index=True)
    market_question: str = ""
    category: str = "other"
    yes_price: float = 0.0
    volume_usd: float = 0.0
    close_time: Optional[datetime] = None
    platform: str = Field(default="polymarket", index=True)  # "polymarket" or "kalshi"
    snapshotted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ResolvedMarket(SQLModel, table=True):
    __tablename__ = "resolved_market"
    market_slug: str = Field(primary_key=True)
    outcome: str = ""
    resolved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MonthlyQuota(SQLModel, table=True):
    __tablename__ = "monthly_quota"
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(index=True)
    year_month: str = ""
    tweets_read: int = 0
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UserSession(SQLModel, table=True):
    __tablename__ = "user_session"
    token: str = Field(primary_key=True)
    username: str = Field(index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PaperTrade(SQLModel, table=True):
    """An open or closed simulated bet that the system would have taken given
    its EV signal and the source's credibility at signal time."""
    __tablename__ = "paper_trade"
    id: Optional[int] = Field(default=None, primary_key=True)
    prediction_id: int = Field(foreign_key="prediction.id", index=True)
    handle: str = Field(index=True)
    market_slug: str = Field(index=True)
    platform: str = Field(default="polymarket")
    bet_side: str = Field(default="YES")
    stake_usd: float = 1.0
    entry_price: float = 0.5  # market YES price at entry, or NO price = 1 - YES if bet_side == "NO"
    entry_ev_score: float = 0.0
    entry_credibility: float = 0.0
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved: bool = False
    resolved_correct: Optional[bool] = None
    pnl_usd: Optional[float] = None
    closed_at: Optional[datetime] = None


class CredibilitySnapshot(SQLModel, table=True):
    __tablename__ = "credibility_snapshot"
    id: Optional[int] = Field(default=None, primary_key=True)
    handle: str = Field(foreign_key="source.handle", index=True)
    snapshotted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    global_credibility: float = 0.0
    category_credibility_json: str = Field(default="{}", sa_column=Column(Text))
    accuracy_unlocked: bool = False

    @property
    def category_credibility(self) -> dict:
        return json.loads(self.category_credibility_json)

    @category_credibility.setter
    def category_credibility(self, value: dict) -> None:
        self.category_credibility_json = json.dumps(value)
