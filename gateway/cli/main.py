"""narve — CLI for the narve.ai gateway engine.

Operates directly on the engine's data layer (gateway/db.py, honouring
DATABASE_URL exactly like the server) and gateway/config.json, and probes the
dashboard fleet over localhost HTTP the same way the gateway's health loop
does. Every surface renders through cli/ui.py, the terminal translation of
the narve.ai design system: monochrome, typography-forward, rank tiers
instead of colour.

Usage:  narve <command> [args]     (narve --help for the full tree)
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(GATEWAY_DIR))

from cli import ui  # noqa: E402


# ── Engine access ────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(GATEWAY_DIR / "config.json") as f:
        return json.load(f)


def get_db():
    """Import the engine's data layer lazily so `narve --help` and config-only
    commands work even when DB deps (psycopg/cryptography) are absent."""
    try:
        import db  # gateway/db.py
    except BaseException as exc:  # BaseException: broken native deps can panic
        print(ui.banner(f"Cannot load the engine data layer: {exc}", "error"))
        sys.exit(2)
    db.init_db()  # idempotent, same as server startup — first run just works
    return db


def resolve_user(db, ident: str):
    """Accept a numeric id, email, or username."""
    user = db.get_user_by_id(int(ident)) if ident.isdigit() else None
    user = user or db.get_user_by_email_or_username(ident)
    if not user:
        print(ui.banner(f"No user matching '{ident}'.", "error"))
        sys.exit(1)
    return user


def dashboard_state(cfg: dict) -> str:
    if cfg.get("merged_into"):
        return "merged"
    if cfg.get("parked"):
        return "parked"
    if cfg.get("hidden") and cfg.get("access_alias"):
        return "companion"
    if cfg.get("hidden"):
        return "delisted"
    return "live"


# Rank tiers for state badges. The rank-1 inverse fill is reserved for alerts
# (DOWN, SUSPENDED, NEW) so a healthy fleet renders quiet.
STATE_RANK = {"live": 2, "delisted": 3, "companion": 3, "parked": 3, "merged": 4}


def probe(port: int, timeout: float = 1.5) -> tuple[bool, float | None]:
    """HTTP GET / against a local dashboard port, like the gateway health loop."""
    start = time.monotonic()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/", headers={"User-Agent": "narve-cli"})
        conn.getresponse().read(0)
        conn.close()
        return True, (time.monotonic() - start) * 1000
    except OSError:
        return False, None


def emit_json(payload) -> None:
    json.dump(payload, sys.stdout, indent=2, default=str)
    print()


# ── fleet ────────────────────────────────────────────────────────────────────

def cmd_fleet(args) -> None:
    cfg = load_config()
    db = get_db()
    subs = db.active_subscription_counts()
    rows, data, total_rate = [], [], 0.0
    for key, d in cfg["dashboards"].items():
        state = dashboard_state(d)
        sub = subs.get(key, {"total": 0, "monthly": 0, "annual": 0})
        rate = (sub["monthly"] * d.get("monthly_cents", 0)
                + sub["annual"] * d.get("annual_cents", 0) / 12)
        total_rate += rate
        healthy, latency = (None, None)
        if not args.no_probe and state not in ("merged", "parked"):
            healthy, latency = probe(d["target"])
        data.append({"key": key, "display_name": d.get("display_name", key),
                     "state": state, "port": d.get("target"),
                     "healthy": healthy, "latency_ms": latency,
                     "subs": sub["total"], "run_rate_cents": rate})
        if healthy is None:
            health = ui.tertiary("—")
        elif healthy:
            health = f"ok {ui.tertiary(f'{latency:.0f}ms')}"
        else:
            health = ui.badge("down", 1)
        rows.append([
            ui.primary(key), d.get("display_name", key),
            ui.badge(state, STATE_RANK[state]), str(d.get("target", "")),
            health, str(sub["total"]), ui.money(rate) if rate else ui.tertiary("—"),
        ])
    if args.json:
        return emit_json(data)
    print()
    print(ui.wordmark())
    print()
    print(ui.table(["key", "dashboard", "state", "port", "health", "subs", "run-rate"],
                   rows, aligns=["l", "l", "l", "r", "l", "r", "r"]))
    print(ui.rule("ghost"))
    live = sum(1 for d in data if d["state"] == "live")
    down = sum(1 for d in data if d["healthy"] is False)
    line = (f"{len(data)} dashboards{ui.dot()}{live} live"
            f"{ui.dot()}monthly run-rate {ui.primary(ui.money(total_rate))}")
    if down:
        line += ui.dot() + ui.badge(f"{down} down", 1)
    print(line)
    print()


# ── health ───────────────────────────────────────────────────────────────────

def cmd_health(args) -> None:
    cfg = load_config()
    targets = [("gateway", cfg.get("gateway_port", 7000))]
    targets += [(k, d["target"]) for k, d in cfg["dashboards"].items()
                if dashboard_state(d) in ("live", "delisted", "companion")]
    rows, data, down = [], [], 0
    for name, port in targets:
        healthy, latency = probe(port)
        data.append({"service": name, "port": port, "healthy": healthy,
                     "latency_ms": latency})
        if healthy:
            status, lat = "ok", ui.tertiary(f"{latency:.0f}ms")
        else:
            status, lat, down = ui.badge("down", 1), ui.tertiary("—"), down + 1
        rows.append([ui.primary(name), str(port), status, lat])
    if args.json:
        return emit_json(data)
    print()
    print(ui.table(["service", "port", "status", "latency"], rows,
                   aligns=["l", "r", "l", "r"]))
    print(ui.rule("ghost"))
    if down:
        print(ui.banner(f"{down} of {len(targets)} services unreachable.", "error"))
    else:
        print(f"all {len(targets)} services reachable")
    print()


# ── dashboards ───────────────────────────────────────────────────────────────

def cmd_dashboards(args) -> None:
    cfg = load_config()
    dashboards = cfg["dashboards"]
    if args.key:
        d = dashboards.get(args.key)
        if not d:
            print(ui.banner(f"No dashboard '{args.key}' in config.json.", "error"))
            sys.exit(1)
        if args.json:
            return emit_json(d)
        state = dashboard_state(d)
        print()
        print(ui.heading(d.get("display_name", args.key)) + "  "
              + ui.badge(state, STATE_RANK[state]))
        print(ui.rule("ghost"))
        pairs = [
            ("key", args.key),
            ("subdomain", f"{d.get('subdomain')}.{cfg.get('domain', '')}"),
            ("port", str(d.get("target", ""))),
            ("description", d.get("description", "")),
            ("monthly", ui.money(d.get("monthly_cents", 0))),
            ("annual", ui.money(d.get("annual_cents", 0))),
            ("websocket", "yes" if d.get("supports_websocket") else "no"),
        ]
        if d.get("merged_into"):
            pairs.append(("merged into", d["merged_into"]))
        if d.get("access_alias"):
            pairs.append(("access alias", d["access_alias"]))
        print(ui.kv(pairs))
        print()
        return
    if args.json:
        return emit_json(dashboards)
    rows = [[ui.primary(k), d.get("display_name", k),
             ui.badge(dashboard_state(d), STATE_RANK[dashboard_state(d)]),
             f"{d.get('subdomain')}.{cfg.get('domain', '')}",
             ui.money(d.get("monthly_cents", 0))]
            for k, d in dashboards.items()]
    print()
    print(ui.table(["key", "dashboard", "state", "subdomain", "monthly"],
                   rows, aligns=["l", "l", "l", "l", "r"]))
    print()


# ── users ────────────────────────────────────────────────────────────────────

ROLE_LABEL = {0: ("user", 3), 1: ("admin", 2), 2: ("super", 1)}


def cmd_users_list(args) -> None:
    db = get_db()
    users = db.list_all_users()
    if args.admins:
        users = [u for u in users if u["is_admin"] > 0]
    if args.suspended:
        users = [u for u in users if u["suspended"]]
    if args.json:
        return emit_json([dict(id=u["id"], email=u["email"], username=u["username"],
                               role=u["is_admin"], suspended=bool(u["suspended"]),
                               created_at=u["created_at"]) for u in users])
    if not users:
        print(ui.empty_state("No users found.",
                             "narve users create --email … --password …"))
        return
    rows = []
    for u in users:
        label, rank = ROLE_LABEL.get(u["is_admin"], ("user", 3))
        flags = ui.badge("suspended", 1) if u["suspended"] else ""
        rows.append([str(u["id"]), ui.primary(u["email"]), u["username"] or ui.tertiary("—"),
                     ui.badge(label, rank), ui.ts(u["created_at"], "%Y-%m-%d"), flags])
    print()
    print(ui.table(["id", "email", "username", "role", "created", ""],
                   rows, aligns=["r", "l", "l", "l", "l", "l"]))
    print(ui.rule("ghost"))
    print(f"{len(rows)} users")
    print()


def cmd_users_show(args) -> None:
    db = get_db()
    u = resolve_user(db, args.ident)
    subs = db.list_subscriptions(u["id"])
    if args.json:
        return emit_json({"user": {k: u[k] for k in u.keys()
                                   if "password" not in k},
                          "subscriptions": [dict(s) for s in subs]})
    label, rank = ROLE_LABEL.get(u["is_admin"], ("user", 3))
    print()
    print(ui.heading(u["email"]) + "  " + ui.badge(label, rank)
          + ("  " + ui.badge("suspended", 1) if u["suspended"] else ""))
    print(ui.rule("ghost"))
    print(ui.kv([
        ("id", str(u["id"])),
        ("username", u["username"] or "—"),
        ("created", ui.ts(u["created_at"])),
        ("default dashboard", u["default_dashboard"] or "—"),
    ]))
    print()
    print(ui.meta("subscriptions"))
    if not subs:
        print(ui.empty_state("No subscriptions.",
                             f"narve grant {u['id']} <dashboard>"))
    else:
        now = time.time()
        rows = []
        for s in subs:
            active = s["status"] == "active" and (not s["expires_at"] or s["expires_at"] > now)
            rows.append([ui.primary(s["dashboard_key"]), s["plan"],
                         ui.badge("active" if active else s["status"], 2 if active else 4),
                         ui.ts(s["expires_at"], "%Y-%m-%d") if s["expires_at"] else "∞",
                         s["source"]])
        print(ui.table(["dashboard", "plan", "status", "expires", "source"], rows))
    print()


def cmd_users_create(args) -> None:
    db = get_db()
    uid = db.create_user(args.email, args.password, username=args.username or "",
                         admin_level=args.role)
    print(ui.banner(f"Created user #{uid} {args.email}"
                    f" (role {ROLE_LABEL.get(args.role, ('user',))[0]}).", "info"))


def cmd_users_flag(args) -> None:
    db = get_db()
    u = resolve_user(db, args.ident)
    action = args.action
    if action == "suspend":
        db.set_user_suspended(u["id"], True)
    elif action == "unsuspend":
        db.set_user_suspended(u["id"], False)
    elif action == "promote":
        db.set_user_role(u["id"], max(u["is_admin"], 1))
    elif action == "demote":
        db.set_user_role(u["id"], 0)
    elif action == "role":
        db.set_user_role(u["id"], args.level)
    print(ui.banner(f"{action} → user #{u['id']} {u['email']}", "info"))


def cmd_users_delete(args) -> None:
    db = get_db()
    u = resolve_user(db, args.ident)
    if not ui.confirm(f"Permanently delete user #{u['id']} {u['email']} and all "
                      f"their sessions/subscriptions?", args.yes):
        sys.exit(1)
    db.delete_user(u["id"])
    print(ui.banner(f"Deleted user #{u['id']} {u['email']}.", "warning"))


# ── subscriptions ────────────────────────────────────────────────────────────

def cmd_subs(args) -> None:
    db = get_db()
    if args.user:
        u = resolve_user(db, args.user)
        subs = [dict(s, email=u["email"], username=u["username"])
                for s in db.list_subscriptions(u["id"])]
    else:
        subs = [dict(s) for s in db.list_all_subscriptions()]
    if args.json:
        return emit_json(subs)
    if not subs:
        print(ui.empty_state("No subscriptions.", "narve grant <user> <dashboard>"))
        return
    now = time.time()
    rows = []
    for s in subs:
        active = s["status"] == "active" and (not s["expires_at"] or s["expires_at"] > now)
        rows.append([ui.primary(s["email"]), s["dashboard_key"], s["plan"],
                     ui.badge("active" if active else s["status"], 2 if active else 4),
                     ui.ts(s["expires_at"], "%Y-%m-%d") if s["expires_at"] else "∞",
                     s["source"]])
    print()
    print(ui.table(["user", "dashboard", "plan", "status", "expires", "source"], rows))
    print(ui.rule("ghost"))
    print(f"{len(rows)} subscriptions")
    print()


def cmd_grant(args) -> None:
    cfg = load_config()
    db = get_db()
    u = resolve_user(db, args.ident)
    keys = set(cfg["dashboards"])
    if args.dashboard not in keys and args.dashboard != "all":
        print(ui.banner(f"Unknown dashboard '{args.dashboard}'. "
                        f"Keys: {', '.join(sorted(keys))}", "error"))
        sys.exit(1)
    targets = sorted(keys) if args.dashboard == "all" else [args.dashboard]
    for key in targets:
        db.upsert_subscription(u["id"], key, plan=args.plan,
                               duration_days=args.days, source="cli")
    span = f"{args.days}d" if args.days else "no expiry"
    print(ui.banner(f"Granted {', '.join(targets)} to {u['email']} "
                    f"({args.plan}, {span}).", "info"))


def cmd_revoke(args) -> None:
    db = get_db()
    u = resolve_user(db, args.ident)
    db.cancel_subscription(u["id"], args.dashboard)
    print(ui.banner(f"Revoked {args.dashboard} from {u['email']}.", "warning"))


# ── invite tokens ────────────────────────────────────────────────────────────

def cmd_tokens(args) -> None:
    db = get_db()
    if args.action == "create":
        token = db.create_invite_token(note=args.note or "", target_email=args.email or "")
        print(ui.banner(f"Invite token: {token}", "info"))
        return
    if args.action == "revoke":
        db.revoke_invite_token(args.id)
        print(ui.banner(f"Revoked invite token #{args.id}.", "warning"))
        return
    tokens = db.list_invite_tokens()
    if args.json:
        return emit_json([dict(t) for t in tokens])
    if not tokens:
        print(ui.empty_state("No invite tokens.", "narve tokens create --note …"))
        return
    rank = {"unclaimed": 2, "claimed": 3, "revoked": 4}
    rows = [[str(t["id"]), ui.primary(t["token"]),
             ui.badge(t["status"], rank.get(t["status"], 3)),
             t["claimed_by_email"] or ui.tertiary("—"),
             t["note"] or ui.tertiary("—"), ui.ts(t["created_at"], "%Y-%m-%d")]
            for t in tokens]
    print()
    print(ui.table(["id", "token", "status", "claimed by", "note", "created"],
                   rows, aligns=["r", "l", "l", "l", "l", "l"]))
    print()


# ── revenue ──────────────────────────────────────────────────────────────────

def cmd_revenue(args) -> None:
    cfg = load_config()
    db = get_db()
    stats = db.get_revenue_stats()
    subs = db.active_subscription_counts()
    per_dash, total_rate = [], 0.0
    for key, d in cfg["dashboards"].items():
        s = subs.get(key)
        if not s:
            continue
        rate = (s["monthly"] * d.get("monthly_cents", 0)
                + s["annual"] * d.get("annual_cents", 0) / 12)
        total_rate += rate
        per_dash.append((key, s, rate))
    if args.json:
        return emit_json({"counts": {k: stats[k] for k in
                                     ("total", "active", "cancelled", "expired")},
                          "monthly_run_rate_cents": total_rate,
                          "per_dashboard": [
                              {"key": k, **s, "run_rate_cents": r}
                              for k, s, r in per_dash]})
    print()
    print(ui.heading("Revenue"))
    print(ui.rule("ghost"))
    print(ui.kv([
        ("active subscriptions", ui.primary(str(stats["active"]))),
        ("cancelled", str(stats["cancelled"])),
        ("expired", str(stats["expired"])),
        ("monthly run-rate", ui.primary(ui.money(total_rate))),
    ]))
    if per_dash:
        print()
        rows = [[ui.primary(k), str(s["total"]), str(s["monthly"]),
                 str(s["annual"]), ui.money(r)]
                for k, s, r in sorted(per_dash, key=lambda x: -x[2])]
        print(ui.table(["dashboard", "subs", "monthly", "annual", "run-rate"],
                       rows, aligns=["l", "r", "r", "r", "r"]))
    print()


# ── enquiries ────────────────────────────────────────────────────────────────

def cmd_enquiries(args) -> None:
    db = get_db()
    if args.read is not None:
        db.mark_enquiry_read(args.read)
        print(ui.banner(f"Enquiry #{args.read} marked read.", "info"))
        return
    rows_db = db.list_enquiries()
    if args.json:
        return emit_json([dict(e) for e in rows_db])
    if not rows_db:
        print(ui.empty_state("No enquiries."))
        return
    rows = [[str(e["id"]),
             ui.badge("new", 1) if not e["read"] else ui.tertiary("read"),
             ui.primary(e["email"]), e["job_title"],
             (e["message"][:60] + "…") if len(e["message"]) > 60 else e["message"],
             ui.ts(e["created_at"], "%Y-%m-%d")]
            for e in rows_db]
    print()
    print(ui.table(["id", "", "email", "role", "message", "date"], rows,
                   aligns=["r", "l", "l", "l", "l", "l"]))
    print()


# ── db ───────────────────────────────────────────────────────────────────────

def cmd_db(args) -> None:
    db = get_db()
    if args.action == "init":
        db.init_db()
        print(ui.banner("Schema initialised / migrated.", "info"))
        return
    with db.conn() as c:
        counts = []
        for tbl in ("users", "sessions", "subscriptions", "invite_tokens",
                    "enquiries", "trading_orders", "user_positions"):
            try:
                n = c.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()
                counts.append((tbl, str(n["n"] if hasattr(n, "keys") else n[0])))
            except Exception:
                counts.append((tbl, "—"))
    backend = "postgresql" if db.IS_PG else f"sqlite ({db.DB_PATH.name})"
    if args.json:
        return emit_json({"backend": backend, "tables": dict(counts)})
    print()
    print(ui.heading("Engine database") + "  " + ui.badge(backend.split(" ")[0], 2))
    print(ui.rule("ghost"))
    print(ui.kv([("backend", backend)] + counts))
    print()


# ── config ───────────────────────────────────────────────────────────────────

def cmd_config(args) -> None:
    cfg = load_config()
    if args.json:
        return emit_json(cfg)
    dashboards = cfg["dashboards"]
    states = {}
    for d in dashboards.values():
        states[dashboard_state(d)] = states.get(dashboard_state(d), 0) + 1
    print()
    print(ui.heading("Gateway configuration"))
    print(ui.rule("ghost"))
    print(ui.kv([
        ("domain", cfg.get("domain", "")),
        ("gateway port", str(cfg.get("gateway_port", ""))),
        ("dashboards", str(len(dashboards))),
        ("states", ui.dot().join(f"{v} {k}" for k, v in sorted(states.items()))),
        ("config file", str(GATEWAY_DIR / "config.json")),
        ("database", "postgresql" if os.environ.get("DATABASE_URL", "").startswith("postgres")
                     else "sqlite"),
    ]))
    print()


# ── argument tree ────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--plain", action="store_true", help="disable ANSI styling")

    p = argparse.ArgumentParser(
        prog="narve", parents=[common],
        description="Command-line interface for the narve.ai gateway engine.",
        epilog="Monochrome by design: state is carried by labels and weight, "
               "never by colour. Set NO_COLOR=1 or pipe output for plain text.")
    class SubParser(argparse.ArgumentParser):
        """Subcommand parser that accepts --json/--plain in any position."""
        def __init__(self, **kw):
            kw.setdefault("parents", []).append(common)
            super().__init__(**kw)

    sub = p.add_subparsers(dest="cmd", metavar="<command>", parser_class=SubParser)

    f = sub.add_parser("fleet", help="dashboard fleet status, health and run-rate")
    f.add_argument("--no-probe", action="store_true", help="skip health probes")
    f.set_defaults(func=cmd_fleet)

    h = sub.add_parser("health", help="probe the gateway and every deployed dashboard")
    h.set_defaults(func=cmd_health)

    d = sub.add_parser("dashboards", help="list or inspect dashboard config")
    d.add_argument("key", nargs="?", help="dashboard key to inspect")
    d.set_defaults(func=cmd_dashboards)

    u = sub.add_parser("users", help="manage engine users")
    us = u.add_subparsers(dest="ucmd", metavar="<action>", required=True)
    ul = us.add_parser("list", help="list users")
    ul.add_argument("--admins", action="store_true")
    ul.add_argument("--suspended", action="store_true")
    ul.set_defaults(func=cmd_users_list)
    ush = us.add_parser("show", help="user detail + subscriptions")
    ush.add_argument("ident", help="user id, email, or username")
    ush.set_defaults(func=cmd_users_show)
    uc = us.add_parser("create", help="create a user")
    uc.add_argument("--email", required=True)
    uc.add_argument("--password", required=True)
    uc.add_argument("--username", default="")
    uc.add_argument("--role", type=int, default=0, choices=(0, 1, 2),
                    help="0 user, 1 admin, 2 super-admin")
    uc.set_defaults(func=cmd_users_create)
    for action in ("suspend", "unsuspend", "promote", "demote"):
        ua = us.add_parser(action, help=f"{action} a user")
        ua.add_argument("ident")
        ua.set_defaults(func=cmd_users_flag, action=action)
    ur = us.add_parser("role", help="set role level explicitly")
    ur.add_argument("ident")
    ur.add_argument("level", type=int, choices=(0, 1, 2))
    ur.set_defaults(func=cmd_users_flag, action="role")
    ud = us.add_parser("delete", help="permanently delete a user")
    ud.add_argument("ident")
    ud.add_argument("--yes", action="store_true", help="skip confirmation")
    ud.set_defaults(func=cmd_users_delete)

    s = sub.add_parser("subs", help="list subscriptions")
    s.add_argument("--user", help="filter to one user (id/email/username)")
    s.set_defaults(func=cmd_subs)

    g = sub.add_parser("grant", help="grant a dashboard subscription")
    g.add_argument("ident", help="user id, email, or username")
    g.add_argument("dashboard", help="dashboard key, or 'all'")
    g.add_argument("--plan", default="invite")
    g.add_argument("--days", type=int, default=None, help="expiry in days (default: none)")
    g.set_defaults(func=cmd_grant)

    r = sub.add_parser("revoke", help="cancel a dashboard subscription")
    r.add_argument("ident")
    r.add_argument("dashboard")
    r.set_defaults(func=cmd_revoke)

    t = sub.add_parser("tokens", help="invite tokens")
    t.add_argument("action", nargs="?", default="list",
                   choices=("list", "create", "revoke"))
    t.add_argument("id", nargs="?", type=int, help="token id (revoke)")
    t.add_argument("--note", default="")
    t.add_argument("--email", default="", help="target email (create)")
    t.set_defaults(func=cmd_tokens)

    rev = sub.add_parser("revenue", help="subscription counts and run-rate")
    rev.set_defaults(func=cmd_revenue)

    e = sub.add_parser("enquiries", help="sales enquiries inbox")
    e.add_argument("--read", type=int, metavar="ID", help="mark an enquiry read")
    e.set_defaults(func=cmd_enquiries)

    dbp = sub.add_parser("db", help="engine database stats / init")
    dbp.add_argument("action", nargs="?", default="stats", choices=("stats", "init"))
    dbp.set_defaults(func=cmd_db)

    c = sub.add_parser("config", help="gateway configuration summary")
    c.set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.json or args.plain:
        ui.disable_ansi()
    if not getattr(args, "cmd", None):
        parser.print_help()
        sys.exit(0)
    if args.cmd == "tokens" and args.action == "revoke" and args.id is None:
        parser.error("tokens revoke requires a token id")
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        # Piped into head/less that exited — not an error.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)


if __name__ == "__main__":
    main()
