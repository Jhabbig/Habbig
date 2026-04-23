# Deploy + environment layout

> **Scope:** the three environments the gateway runs in, where their `.env`
> files live, and the step-by-step startup checks. Companion to
> `SECRETS.md` (what's in the env files) and `gateway/config.py` (what's
> enforced at boot).

## Environments

narve.ai runs in three logical environments. Today only `production` has
a dedicated `.env`; this doc is the template for when `staging` lands.

| Environment | `.env` path | Who edits it | Owner | Notes |
|---|---|---|---|---|
| `local` (dev) | `gateway/.env` on your laptop, gitignored | You | — | Copy `gateway/.env.example`; set `PRODUCTION=0`; leave optional Stripe/Anthropic blank unless testing those paths. |
| `staging` (future) | `gateway/.env.staging` on the staging box, 600 perms | Infra team | Staging server | Mirrors production keys but points at `sk_test_*` Stripe + Anthropic's cheaper tier. |
| `production` | `gateway/.env` on `julianhabbig@100.69.44.108`, 600 perms | Infra team | `julianhabbig` user | `PRODUCTION=1`. Every `REQUIRED` var in `gateway/config.py` must be set; the validator `sys.exit(2)`s otherwise. |

## Directory layout on the production box

```
/home/julianhabbig/Habbig/
├── .env                     # 600, owned by julianhabbig. NEVER in git.
├── .env.backup-YYYYMMDD     # Pre-rotation snapshot. 600. Delete after verify.
├── gateway/
│   ├── auth.db              # SQLite — 640 so root-group read for backups.
│   ├── server.py
│   ├── config.py            # Runs validate_config() at startup.
│   └── …
└── logs/
    └── gateway.out          # Structured JSON; shipped to BetterStack.
```

## Startup sanity — daily one-liner

```bash
ssh julianhabbig@100.69.44.108 "cd ~/Habbig && {
  test -f .env                      && echo 'env: present'      || echo 'env: MISSING'
  [ \"$(stat -c '%a' .env)\" = '600' ] && echo 'perms: 600 OK'   || echo 'perms: NOT 600'
  cd gateway && python3 -c 'import config; \
    errs = config.validate_config(); print(\"config: OK\" if not errs else \"config: \" + str(len(errs)) + \" errors\")'
}"
```

Any `MISSING`, `NOT 600`, or non-zero error count = investigate before next
incident hits.

## First-time provisioning checklist

When setting up a new server (staging or a replacement prod box):

1. Clone the repo: `git clone git@github.com:Jhabbig/Habbig.git`.
2. Copy `.env.example`:
   ```bash
   cp gateway/.env.example gateway/.env
   chmod 600 gateway/.env
   ```
3. Fill in every `REQUIRED` value per `SECRETS.md` generation commands.
4. Dry-run the validator:
   ```bash
   cd gateway && python3 -c 'import config; errs = config.validate_config(); \
     [print(e) for e in errs]; print("ok" if not errs else "FAIL")'
   ```
5. Start the server (systemd unit or `uvicorn server:app`).
6. Hit `/healthz` from localhost — expect `200 OK`.
7. Hit `/gate` from an external IP, submit the gate password, confirm the
   `/dashboards` redirect lands.
8. Record the deploy in `SECURITY_LOG.md` (environment, date, operator).

## Missing-var detector

Shell pipeline to diff required vars against what's actually in production:

```bash
ssh julianhabbig@100.69.44.108 "cd ~/Habbig && \
  comm -23 \
    <(grep -E '^[A-Z_]' gateway/.env.example | grep -i REQUIRED | cut -d= -f1 | sort) \
    <(grep -E '^[A-Z_]' gateway/.env | cut -d= -f1 | sort)"
# Any output = a REQUIRED var is missing in production.
```

Pair with the runtime validator in `config.py`; that one catches shape
mismatches, this one catches omissions.

## File permissions

```
.env            600  julianhabbig:julianhabbig
auth.db         640  julianhabbig:julianhabbig   (so backup user can read)
gateway/*.py    644  (code; read-only for non-owners)
```

If `stat -c '%a' .env` returns anything other than `600`:

```bash
chmod 600 ~/Habbig/.env
```

## Rollback

Every rotation must keep the previous value stashed:

```bash
cp ~/Habbig/.env ~/Habbig/.env.backup-$(date +%Y%m%d)
```

If the new value breaks something the startup validator missed, restore:

```bash
cp ~/Habbig/.env.backup-YYYYMMDD ~/Habbig/.env
sudo systemctl restart narve-gateway   # or equivalent
```

Delete the backup once the new value has been in production for 24h
without incident.

## Related

- `gateway/.env.example` — variable catalogue (REQUIRED / OPTIONAL).
- `gateway/config.py` — `validate_config()` startup enforcement.
- `SECRETS.md` — rotation runbook + generation commands per secret.
- `SECURITY_LOG.md` — append-only log of rotations + incidents.
- `.github/workflows/secret-scan.yml` — TruffleHog CI.
