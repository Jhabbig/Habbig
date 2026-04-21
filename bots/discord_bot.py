"""Discord bot — standalone long-running process.

Run on the server:

    nohup python3 bots/discord_bot.py > /tmp/discord_bot.log 2>&1 &

Requires ``discord.py==2.3.2``, DISCORD_BOT_TOKEN, DISCORD_APPLICATION_ID.

Slash commands:
  /narve best                  — top EV bets
  /narve market <slug-or-url>  — bundle for a specific market
  /narve source <handle>       — source credibility card
  /narve setup                 — configure alert channel (admin only)

The ``/narve setup`` command designates the alert channel for the
server and writes the ``discord_servers`` row. ``/narve best`` and
friends DM the user for a personal view; unsolicited broadcasts go to
the configured alert channel.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "gateway"))

from bots.formatters import (  # noqa: E402
    format_best_bet_discord,
    load_best_bets,
)


log = logging.getLogger("discord_bot")


DISCORD_TOKEN_ENV = "DISCORD_BOT_TOKEN"
APP_URL_ENV = "APP_URL"


def _app_url() -> str:
    return os.environ.get(APP_URL_ENV, "https://narve.ai").rstrip("/")


def _guild_row(guild_id: int) -> Optional[dict]:
    import db
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM discord_servers WHERE guild_id = ?",
            (str(guild_id),),
        ).fetchone()
    return dict(row) if row else None


def _upsert_guild(
    *, guild_id: int, channel_id: int, setup_by_user_id: Optional[int] = None,
) -> None:
    import db
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO discord_servers "
            "(guild_id, alert_channel_id, setup_by_user_id, connected_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "  alert_channel_id = excluded.alert_channel_id, "
            "  setup_by_user_id = excluded.setup_by_user_id, "
            "  is_active = 1",
            (str(guild_id), str(channel_id), setup_by_user_id, now),
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    token = os.environ.get(DISCORD_TOKEN_ENV, "").strip()
    if not token:
        log.error("%s not set — bot will not start", DISCORD_TOKEN_ENV)
        sys.exit(1)

    import discord  # type: ignore[import]
    from discord import app_commands  # type: ignore[import]

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    narve_group = app_commands.Group(
        name="narve", description="narve.ai prediction-market intelligence",
    )

    @narve_group.command(name="best", description="Top EV bets right now")
    async def _best(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        bets = load_best_bets(limit=5)
        if not bets:
            await interaction.followup.send(
                "No fresh high-EV bets right now.", ephemeral=True,
            )
            return
        embeds = [discord.Embed(**format_best_bet_discord(b)) for b in bets[:5]]
        await interaction.followup.send(embeds=embeds, ephemeral=True)

    @narve_group.command(name="market", description="Look up a market")
    @app_commands.describe(slug_or_url="Polymarket slug or full URL")
    async def _market(interaction: discord.Interaction, slug_or_url: str):
        await interaction.response.defer(ephemeral=True)
        slug = slug_or_url
        if "polymarket.com/event/" in slug:
            try:
                slug = slug.split("/event/", 1)[1].split("?", 1)[0].strip("/")
            except Exception:
                pass
        try:
            from extension_routes import _compose_bundle  # type: ignore[import]
            bundle = await _compose_bundle(slug)
        except Exception as exc:
            log.warning("market lookup failed: %s", exc)
            bundle = None
        if not bundle:
            await interaction.followup.send(
                "No narve coverage for that market.", ephemeral=True,
            )
            return
        edge = bundle.get("betyc_edge")
        embed = discord.Embed(**format_best_bet_discord({
            "market_slug": slug,
            "question": bundle.get("market_question") or slug,
            "betyc_probability": bundle.get("betyc_yes_probability"),
            "market_price": bundle.get("market_yes_price"),
            "edge_pct": (edge * 100) if edge is not None else None,
            "confidence": bundle.get("betyc_confidence"),
            "source_count": bundle.get("source_count") or 0,
            "top_sources": bundle.get("top_sources") or [],
            "side": "yes" if (edge or 0) > 0 else "no",
            "category": "market",
        }))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @narve_group.command(name="source", description="Source credibility card")
    @app_commands.describe(handle="Source handle, e.g. @PredictIt")
    async def _source(interaction: discord.Interaction, handle: str):
        handle = handle.lstrip("@")
        await interaction.response.defer(ephemeral=True)
        try:
            import db
            cred = db.get_source_credibility(handle) if hasattr(
                db, "get_source_credibility"
            ) else None
        except Exception:
            cred = None
        if not cred:
            await interaction.followup.send(
                f"No rated source @{handle}.", ephemeral=True,
            )
            return
        score = round(float(cred["global_credibility"] or 0), 2)
        total = int(cred["total_predictions"] or 0)
        correct = int(cred["correct_predictions"] or 0)
        accuracy = (correct / total) if total else 0.0
        embed = discord.Embed(
            title=f"@{handle}",
            url=f"{_app_url()}/sources/{handle}",
            description="narve.ai credibility card",
            color=0x9AA0A6,
        )
        embed.add_field(name="Credibility", value=f"{score:.2f}", inline=True)
        embed.add_field(name="Accuracy", value=f"{accuracy * 100:.0f}%",
                        inline=True)
        embed.add_field(name="Tracked", value=str(total), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @narve_group.command(name="setup", description="Designate the alert channel (admin only)")
    async def _setup(interaction: discord.Interaction):
        # Admin-only — ``manage_guild`` permission is the practical
        # "is an admin in this server" bit. We also require the command
        # to be run in a guild channel, not a DM.
        if interaction.guild is None:
            await interaction.response.send_message(
                "Run this in the server you want to set up.", ephemeral=True,
            )
            return
        member = interaction.user
        if not (
            isinstance(member, discord.Member)
            and member.guild_permissions.manage_guild
        ):
            await interaction.response.send_message(
                "You need Manage Server permission to run /narve setup.",
                ephemeral=True,
            )
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Run this inside the text channel narve should post to.",
                ephemeral=True,
            )
            return
        _upsert_guild(
            guild_id=interaction.guild.id,
            channel_id=channel.id,
            setup_by_user_id=None,
        )
        await interaction.response.send_message(
            f"narve will post alerts to #{channel.name}. "
            f"Tune thresholds at {_app_url()}/settings/discord.",
            ephemeral=True,
        )

    tree.add_command(narve_group)

    @client.event
    async def on_ready():
        await tree.sync()
        log.info("Discord bot ready as %s", client.user)

    log.info("Discord bot starting")
    client.run(token)


if __name__ == "__main__":
    main()
