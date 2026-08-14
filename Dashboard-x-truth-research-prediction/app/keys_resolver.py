"""Resolve scraper credentials at runtime.

Order of precedence: process env vars → first User row with stored creds.
Per-user creds are Fernet-encrypted in the DB, so we decrypt them here.

This makes the per-user "Profile → API Keys" feature actually work — before
this module the pipeline only ever read env vars and silently ignored the
encrypted user-supplied tokens.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlmodel import select

from app.config import settings
from app.db import AsyncSession, engine
from app.models import User
from app.security import decrypt_field

logger = logging.getLogger(__name__)


@dataclass
class TwitterCreds:
    bearer_token: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.bearer_token)


@dataclass
class TruthSocialCreds:
    username: str = ""
    password: str = ""
    access_token: str = ""
    api_base_url: str = "https://truthsocial.com"

    @property
    def usable(self) -> bool:
        return bool(self.access_token) or bool(self.username and self.password)


async def _first_user_with_creds() -> Optional[User]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        # Prefer admin if it has creds; otherwise the first user that does.
        stmt = select(User).where(
            (User.twitter_bearer_token != "")
            | (User.truthsocial_access_token != "")
            | ((User.truthsocial_username != "") & (User.truthsocial_password != ""))
        )
        result = await session.exec(stmt)
        return result.first()


async def resolve_twitter_creds() -> TwitterCreds:
    env_token = settings.get("TWITTER_BEARER_TOKEN", "") or ""
    if env_token:
        return TwitterCreds(bearer_token=env_token)
    user = await _first_user_with_creds()
    if user and user.twitter_bearer_token:
        return TwitterCreds(bearer_token=decrypt_field(user.twitter_bearer_token))
    return TwitterCreds()


async def resolve_truthsocial_creds() -> TruthSocialCreds:
    env = TruthSocialCreds(
        username=settings.get("TRUTHSOCIAL_USERNAME", "") or "",
        password=settings.get("TRUTHSOCIAL_PASSWORD", "") or "",
        access_token=settings.get("TRUTHSOCIAL_ACCESS_TOKEN", "") or "",
        api_base_url=settings.get("TRUTHSOCIAL_API_BASE_URL", "https://truthsocial.com"),
    )
    if env.usable:
        return env
    user = await _first_user_with_creds()
    if user:
        return TruthSocialCreds(
            username=user.truthsocial_username or "",
            password=decrypt_field(user.truthsocial_password) if user.truthsocial_password else "",
            access_token=decrypt_field(user.truthsocial_access_token) if user.truthsocial_access_token else "",
            api_base_url=env.api_base_url,
        )
    return env
