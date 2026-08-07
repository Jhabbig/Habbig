#!/bin/bash
#
# Create one virtualenv per service under deploy/venvs/<service>/ from that
# service's pinned requirements.txt, so services stop sharing one venv.
# Idempotent: re-running only reinstalls when the requirements file changed
# (sha256 stamp inside the venv).
#
# Run on the prod box as the service user:
#   bash deploy/bootstrap-venvs.sh                      # all SERVICES in sites.conf
#   bash deploy/bootstrap-venvs.sh narve-gateway ...    # just the named units
#
# Overridable for local dry-runs (never needed in prod):
#   NARVE_VENVS_DIR  where venvs are created (default: deploy/venvs)
#   NARVE_PYTHON     interpreter used to seed venvs (default: python3)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
VENVS_DIR="${NARVE_VENVS_DIR:-$SCRIPT_DIR/venvs}"
PYTHON_BIN="${NARVE_PYTHON:-python3}"

# shellcheck source=sites.conf
source "$SCRIPT_DIR/sites.conf"

# case instead of declare -A so macOS bash 3.2 can dry-run this script.
service_req_dir() {
    case "$1" in
        narve-gateway)      echo "gateway" ;;
        narve-crypto)       echo "crypto-dashboard" ;;
        narve-sports)       echo "sports-dashboard" ;;
        narve-weather)      echo "polymarket_weather_dashboard" ;;
        narve-world)        echo "world-state-dashboard" ;;
        narve-midterm)      echo "midterm-dashboard/backend" ;;
        narve-traders)      echo "top-traders-dashboard" ;;
        narve-stock)        echo "stock-dashboard" ;;
        narve-climate)      echo "climate-dashboard" ;;
        narve-centralbank)  echo "centralbank-dashboard" ;;
        narve-voters)       echo "voters-dashboard" ;;
        narve-world-health) echo "world-health-dashboard" ;;
        *)                  echo "" ;;
    esac
}

hash_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

services=("$@")
if [ ${#services[@]} -eq 0 ]; then
    services=("${SERVICES[@]}")
fi

mkdir -p "$VENVS_DIR"
failed=0
for svc in "${services[@]}"; do
    req_dir="$(service_req_dir "$svc")"
    if [ -z "$req_dir" ]; then
        echo "SKIP $svc: no requirements mapping (non-python unit?)"
        continue
    fi
    req_file="$REPO_DIR/$req_dir/requirements.txt"
    if [ ! -f "$req_file" ]; then
        echo "FAIL $svc: $req_file not found"
        failed=1
        continue
    fi
    venv_dir="$VENVS_DIR/$svc"
    if [ ! -x "$venv_dir/bin/python" ]; then
        echo "CREATE $svc: new venv at $venv_dir"
        "$PYTHON_BIN" -m venv "$venv_dir"
    fi
    stamp="$venv_dir/.requirements.sha256"
    want="$(hash_file "$req_file")"
    have=""
    [ -f "$stamp" ] && have="$(cat "$stamp")"
    if [ "$want" = "$have" ]; then
        echo "OK $svc: requirements unchanged"
        continue
    fi
    echo "INSTALL $svc <- $req_dir/requirements.txt"
    "$venv_dir/bin/pip" install --quiet --upgrade pip
    if "$venv_dir/bin/pip" install --quiet -r "$req_file"; then
        printf '%s\n' "$want" > "$stamp"
        echo "DONE $svc"
    else
        echo "FAIL $svc: pip install failed"
        failed=1
    fi
done
exit $failed
