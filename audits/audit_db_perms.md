# Audit: Gateway SQLite DB File Permissions

- **Date (UTC):** 2026-05-15T13:04:05Z
- **Host:** `julianhabbig@100.69.44.108` (production)
- **Scope:** `~/Habbig/gateway/auth.db` and SQLite sidecar files (`-wal`, `-shm`, `-journal`)
- **Method:** SSH; synchronous `ls -la`, `stat`, `find` only. No writes performed on the server.

## Required posture

| File | Required mode | Required owner |
|---|---|---|
| `auth.db` | `0600` | `julianhabbig:julianhabbig` |
| `auth.db-wal` (when present) | `0600` | `julianhabbig:julianhabbig` |
| `auth.db-shm` (when present) | `0600` | `julianhabbig:julianhabbig` |
| `auth.db-journal` (when present) | `0600` | `julianhabbig:julianhabbig` |

Rationale: `auth.db` contains sessions, password hashes, and Stripe-linked customer state. Mode `0600` is the only acceptable posture on a single-tenant server; any group/world read is a credential exposure.

## Observed (production)

```
/home/julianhabbig/Habbig/gateway/auth.db                              0600 julianhabbig:julianhabbig  122,937,344 bytes  (mtime 2026-05-15 14:04 local)
/home/julianhabbig/Habbig/gateway/auth.db-wal                          MISSING
/home/julianhabbig/Habbig/gateway/auth.db-shm                          MISSING
/home/julianhabbig/Habbig/gateway/auth.db-journal                      MISSING
```

Additional SQLite-adjacent files found in the same directory:

```
/home/julianhabbig/Habbig/gateway/auth.db.backup-pre-188-20260515-0004 0600 julianhabbig:julianhabbig  133,255,168 bytes
/home/julianhabbig/Habbig/gateway/auth-staging.db                      0644 julianhabbig:julianhabbig    1,306,624 bytes
/home/julianhabbig/Habbig/gateway/db.db                                0644 julianhabbig:julianhabbig            0 bytes
/home/julianhabbig/Habbig/gateway/._db.db                              0644 julianhabbig:julianhabbig          163 bytes
```

Containing directory: `/home/julianhabbig/Habbig/gateway` is `0755 julianhabbig:julianhabbig`.

Process umask in the deploy user's interactive shell: `0002` (default-group-writable for new files — see Gap 4).

## Result

### Per-file verdict

| File | Mode | Owner | Verdict |
|---|---|---|---|
| `auth.db` | `0600` | `julianhabbig:julianhabbig` | PASS |
| `auth.db-wal` | n/a (absent) | n/a | PASS (not present at audit time) |
| `auth.db-shm` | n/a (absent) | n/a | PASS (not present at audit time) |
| `auth.db-journal` | n/a (absent) | n/a | PASS (not present at audit time) |
| `auth.db.backup-pre-188-20260515-0004` | `0600` | `julianhabbig:julianhabbig` | PASS |
| `auth-staging.db` | `0644` | `julianhabbig:julianhabbig` | **FAIL** — world-readable |
| `db.db` (empty stray) | `0644` | `julianhabbig:julianhabbig` | FAIL (stray; should be removed) |
| `._db.db` (macOS resource fork stray) | `0644` | `julianhabbig:julianhabbig` | FAIL (stray; should be removed) |

**Hard rule held:** production `auth.db` is `0600`, owned by `julianhabbig`. Primary check passes.

## Gaps

1. **`auth-staging.db` is `0644` (world-readable)** on the production host. Even if it only contains test data, it lives next to the real DB, ships with the same backups, and any future code path that points at it inherits world-readable persistence. Chmod to `0600` and confirm whether it should exist on the production box at all (it likely belongs only on a staging host).

2. **Stray `db.db` (0 bytes) and `._db.db` (macOS AppleDouble metadata)** at `0644` in `gateway/`. These are leftover from a local-to-server rsync without `--exclude='._*'`. Delete both and add `._*` to the deploy exclude list.

3. **WAL/SHM not observable at audit time.** The DB is clearly being written (mtime is current), so the absence of `-wal`/`-shm` means either (a) the connection was just checkpointed and closed, or (b) the connection is using a non-WAL journal mode. We could not confirm `PRAGMA journal_mode` — `sqlite3` is not installed on the server. If WAL is in use, sidecar files will reappear under load and their mode is governed by the process umask, which is `0002` — so a freshly created `-wal` would land at `0664`, not `0600`. Recommend either (i) install `sqlite3` on the server so this audit can confirm `journal_mode` synchronously, or (ii) explicitly set the SQLite open mode/umask in the gateway connection setup so sidecars are created `0600` regardless of shell umask. Also: SQLite WAL inherits its mode from the main DB at creation time in most builds, but this is a brittle assumption to rely on — pin it.

4. **Shell umask is `0002`** for the `julianhabbig` user. Any new file created interactively in `gateway/` will be group-writable by default. Recommend setting `umask 0077` in `~/.bashrc` / the systemd unit's `UMask=0077` directive for the gateway service so the process-created `-wal`/`-shm`/`-journal` files cannot be group/world readable even transiently.

5. **Directory `gateway/` is `0755`.** Acceptable on a single-user box, but combined with the `0644` stragglers above it means any process running as another local user (none today, but a future ops user / monitoring agent) could read them. Consider tightening to `0750` once you confirm no service account outside `julianhabbig:julianhabbig` needs to traverse it.

6. **Audit could not be re-run automatically.** No cron/systemd-timer was found that periodically re-checks these modes. Add a daily check that fails loud if `auth.db*` ever drifts off `0600`.

## Recommended remediation (informational — not executed, pre-release rules in force)

```
chmod 0600 ~/Habbig/gateway/auth-staging.db
rm -f ~/Habbig/gateway/db.db ~/Habbig/gateway/._db.db
# In the gateway systemd unit: UMask=0077
# In the SQLite connection bootstrap: explicit os.chmod after open, or open with O_CREAT mode 0600
```
