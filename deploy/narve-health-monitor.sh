#!/usr/bin/env bash
# narve-health-monitor.sh — periodic platform health probe.
#
# Designed for user crontab (no sudo needed for read-only checks):
#   */5 * * * * /home/julianhabbig/Polymarket/deploy/narve-health-monitor.sh >> /tmp/narve-health.log 2>&1
#
# Each run produces one log line per check + a summary state file at
# /tmp/narve-status.<level> where <level> is ok / warn / crit. The user
# can `cat /tmp/narve-status.crit` to see whether anything's on fire.
# Stale state files are pruned each cycle so a recovered service drops
# its previous warn/crit marker.
#
# What it watches:
#   1. gateway/.env.production exists AND contains ODDS_API_KEY (was wiped
#      twice by deploy.sh --delete; that root-cause is now fixed in
#      deploy.sh itself, but we keep the watch as a tripwire).
#   2. Each narve-* systemd service is active.
#   3. Each public dashboard URL responds 200/302 (proves Cloudflare can
#      reach the gateway and the gateway can reach the backend).
#   4. sports /api/health: status field. When it flips to
#      "degraded-quota-exhausted" we surface that as a warn so the user
#      knows to top up the-odds-api credits before the demo.
#
# Exit code: 0 always (cron ignores it), but the log + state file carry
# the signal.

set -u

LOG="/tmp/narve-health.log"
STATE_PREFIX="/tmp/narve-status"
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ENV_FILE="/home/julianhabbig/Polymarket/gateway/.env.production"
# Live products only — parked services (sports, world, traders, centralbank,
# crypto-trackers, whale) are intentionally stopped and merged ones
# (disasters, climate) redirect, so probing them would be pure noise.
SERVICES=(crypto midterm stock weather)
DASHBOARDS=(weather.narve.ai crypto.narve.ai midterm.narve.ai stocks.narve.ai)
WORST_LEVEL=ok  # ok < warn < crit

bump() {
    case "$1:$WORST_LEVEL" in
        crit:*)   WORST_LEVEL=crit;;
        warn:ok)  WORST_LEVEL=warn;;
        warn:*)   ;;  # crit beats warn
        ok:*)     ;;
    esac
}

log() { echo "[$NOW] $1" | tee -a "$LOG"; }

# Clean prior state markers (so recovered services stop showing warnings)
rm -f "${STATE_PREFIX}.warn" "${STATE_PREFIX}.crit" 2>/dev/null

# 1. .env.production sanity
if [ ! -f "$ENV_FILE" ]; then
    log "CRIT  env-file missing: $ENV_FILE — sports + gateway will start without API keys"
    bump crit
    echo "$NOW env-file missing" >> "${STATE_PREFIX}.crit"
elif ! grep -q "^ODDS_API_KEY=." "$ENV_FILE" 2>/dev/null; then
    log "WARN  env-file missing ODDS_API_KEY"
    bump warn
    echo "$NOW env-file missing ODDS_API_KEY" >> "${STATE_PREFIX}.warn"
elif ! grep -q "^GATEWAY_SSO_SECRET=." "$ENV_FILE" 2>/dev/null; then
    log "WARN  env-file missing GATEWAY_SSO_SECRET"
    bump warn
    echo "$NOW env-file missing GATEWAY_SSO_SECRET" >> "${STATE_PREFIX}.warn"
else
    log "OK    env-file present with required keys"
fi

# 2. systemd services active
for s in "${SERVICES[@]}"; do
    state=$(systemctl is-active "narve-$s" 2>&1)
    if [ "$state" = "active" ]; then
        log "OK    service narve-$s active"
    else
        log "CRIT  service narve-$s state=$state"
        bump crit
        echo "$NOW narve-$s $state" >> "${STATE_PREFIX}.crit"
    fi
done

# 3. Public dashboards reachable
for url in "${DASHBOARDS[@]}"; do
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "https://$url/")
    case "$code" in
        200|302)
            log "OK    public https://$url → HTTP $code"
            ;;
        000)
            log "CRIT  public https://$url → no response (timeout / DNS)"
            bump crit
            echo "$NOW $url no response" >> "${STATE_PREFIX}.crit"
            ;;
        *)
            log "WARN  public https://$url → HTTP $code"
            bump warn
            echo "$NOW $url HTTP $code" >> "${STATE_PREFIX}.warn"
            ;;
    esac
done

# 4. Worst-level summary
log "SUMMARY worst=$WORST_LEVEL"
echo "$NOW $WORST_LEVEL" > "${STATE_PREFIX}.${WORST_LEVEL}"

# Trim the log to the last ~5000 lines (each cycle adds ~15 lines, so this
# is roughly a week of history)
if [ -f "$LOG" ]; then
    tail -5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
