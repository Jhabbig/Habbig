# SEV-1 — Site down

Symptoms: `narve.ai` returns 5xx / times out, or `/health` is
unreachable. Paged by user report, uptime monitor, or Cloudflare edge.

## First 5 minutes

**Confirm externally.**

```bash
curl -m 5 -sI https://narve.ai | head -1
curl -m 5 -s https://narve.ai/health
```

If both time out, it might be DNS / Cloudflare (skip to the Cloudflare
section below). If you get 5xx bodies, the origin is running but broken
— continue.

**SSH + process check.**

```bash
ssh julianhabbig@100.69.44.108
ps -ef | grep 'uvicorn server:app' | grep -v grep
```

No uvicorn row → crashed. One row → process is alive but something
downstream is broken; read the log anyway.

## If uvicorn isn't running

```bash
tail -200 /tmp/gateway.log   # look for the last stack trace
```

Common causes:

| Log line | Cause | Fix |
| --- | --- | --- |
| `FATAL: PRODUCTION=1 but GATEWAY_COOKIE_SECRET is unset` | env file missing the key | `grep GATEWAY_COOKIE_SECRET ~/.gateway_env` — if missing, generate + add + restart |
| `migration upgrade failed at startup: no such column: ...` | schema drift on the live DB | see `database_corruption.md` and hand-patch the ALTER TABLE |
| `Address already in use` | port 7000 still held | `fuser -k 7000/tcp; sleep 2` then restart |
| `ImportError: cannot import name ...` | bad deploy | revert to prior commit (`git reset --hard HEAD~1`) and restart |

**Standard restart:**

```bash
cd ~/Habbig/gateway
fuser -k 7000/tcp 2>/dev/null; sleep 2
set -a
source ~/.gateway_env
set +a
export PRODUCTION=1
nohup python3 -m uvicorn server:app --host 127.0.0.1 --port 7000 \
  > /tmp/gateway.log 2>&1 &
disown
sleep 5
curl -s -H "CF-Connecting-IP: 127.0.0.1" http://127.0.0.1:7000/health
```

Expect `{"status":"ok", ...}` within 5–10 seconds of the restart.

## If uvicorn is running but returning 5xx

```bash
tail -200 /tmp/gateway.log | grep -E "ERROR|Traceback|500"
```

The Global Exception Handler in `server.py:_global_exception_handler`
logs every 500 with a full traceback. Find the first trace in the
current window; the fix is usually a bad recent commit. Revert:

```bash
cd ~/Habbig
git log --oneline -5
git reset --hard <known-good-sha>
# then re-run the standard restart above.
```

## If Cloudflare tunnel is the culprit

```bash
systemctl status cloudflared
journalctl -u cloudflared --since "10 minutes ago" | tail -50
```

If the tunnel is down:

```bash
sudo systemctl restart cloudflared
sleep 5
systemctl status cloudflared
curl -m 5 -sI https://narve.ai | head -1
```

If `cloudflared` is healthy but narve.ai still times out, the issue is
at the Cloudflare edge — see [`cloudflare_incident.md`](cloudflare_incident.md).

## If the database is locked

`PRAGMA integrity_check` will stall if a writer is stuck.

```bash
lsof ~/Habbig/gateway/auth.db | head -20
```

Identify the holder. If it's a zombie python process from a previous
crash, `kill -9 <pid>` is safe (WAL mode guarantees the write log
recovers). If it's the current uvicorn, restart per above.

**Last resort only** (loses uncheckpointed WAL writes — never more
than a few seconds' worth at our scale):

```bash
sqlite3 ~/Habbig/gateway/auth.db "PRAGMA journal_mode=DELETE; PRAGMA journal_mode=WAL"
```

## If port 7000 is stolen by something else

The legacy Polymarket gateway's systemd unit has shown up historically
after a host reboot:

```bash
systemctl list-units --type=service | grep -i polymarket
sudo systemctl disable --now polymarket-gateway
```

Then restart our uvicorn.

## Verify before clearing the incident

```bash
curl -m 5 -s https://narve.ai/health | python3 -m json.tool
```

Every check must read `"ok"`:

```
"checks":{"database":"ok","static":"ok","dashboards":"ok",
          "encryption":"ok","gate":"ok","email":"unconfigured"}
```

(Email `"unconfigured"` is fine — the prod box is intentionally
email-dry-run.)

## Escalate

If the site is still down **30 minutes** after the page, write a one-
line status to `/status` manually and start the full-rollback
procedure in [`../RUNBOOK.md`](../RUNBOOK.md) ("Deploy a change" →
run it in reverse with the prior sha).

## Postmortem

Any SEV-1 must produce a [`postmortem_template.md`](postmortem_template.md)
writeup within 48 hours. File it at `postmortems/YYYY-MM-DD-slug.md`.
