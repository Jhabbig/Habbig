#!/usr/bin/env bash
# ─────────────────────────────────────────────────
# snapshot.sh — Save and restore website states
# ─────────────────────────────────────────────────
# Usage:
#   ./snapshot.sh save [site]        Save a snapshot (all sites or one)
#   ./snapshot.sh list [site]        List saved snapshots
#   ./snapshot.sh restore <id>       Restore a snapshot
#   ./snapshot.sh diff <id>          Show what changed since a snapshot
#   ./snapshot.sh delete <id>        Delete a snapshot
#   ./snapshot.sh prune [n]          Keep only the last n snapshots per site (default: 10)
#
# Examples:
#   ./snapshot.sh save                     # snapshot everything
#   ./snapshot.sh save gateway             # snapshot just the gateway
#   ./snapshot.sh save -m "before styling" # snapshot with a note
#   ./snapshot.sh list                     # see all snapshots
#   ./snapshot.sh restore 3               # restore snapshot #3
#   ./snapshot.sh diff 3                   # see what changed vs snapshot #3

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SNAPSHOT_DIR="$SCRIPT_DIR/.snapshots"

# Sites to back up (directories that contain actual website/dashboard code)
SITES=(
    gateway
    crypto-dashboard
    stock-dashboard
    sports-dashboard
    polymarket_weather_dashboard
    world-state-dashboard
    midterm-dashboard
    Dashboard-x-truth-research-prediction
    polymarket-bot
    polymarket_weather_bot
)

# Excluded from snapshots (patterns for tar — databases handled separately)
EXCLUDES=(
    "__pycache__"
    "*.pyc"
    ".DS_Store"
    "node_modules"
    "venv"
    ".venv"
    "*.log"
    "*.db"
    "*.db-wal"
    "*.db-shm"
    "*.sqlite"
    "*.sqlite3"
)

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

mkdir -p "$SNAPSHOT_DIR"

# ── Helpers ──────────────────────────────────────

build_exclude_args() {
    local args=()
    for pat in "${EXCLUDES[@]}"; do
        args+=(--exclude="$pat")
    done
    echo "${args[@]}"
}

# Safely backup SQLite databases using sqlite3 .backup (online, no locking)
backup_databases() {
    local target_dir="$1"  # where to put the db copies
    local scope="$2"       # "all" or a site name

    mkdir -p "$target_dir"

    local sites_to_scan=()
    if [[ "$scope" == "all" ]]; then
        sites_to_scan=("${SITES[@]}")
    else
        sites_to_scan=("$scope")
    fi

    local db_count=0
    for s in "${sites_to_scan[@]}"; do
        [[ -d "$SCRIPT_DIR/$s" ]] || continue
        # Find all .db, .sqlite, .sqlite3 files in this site
        while IFS= read -r db_file; do
            [[ -z "$db_file" ]] && continue
            # Relative path from project root
            local rel_path="${db_file#$SCRIPT_DIR/}"
            local dest="$target_dir/$rel_path"
            mkdir -p "$(dirname "$dest")"

            # Use sqlite3 .backup for a safe online copy
            if command -v sqlite3 &>/dev/null; then
                sqlite3 "$db_file" ".backup '$dest'" 2>/dev/null
            else
                # Fallback: copy (less safe but better than nothing)
                cp "$db_file" "$dest"
            fi
            db_count=$((db_count + 1))
        done < <(find "$SCRIPT_DIR/$s" -type f \( -name "*.db" -o -name "*.sqlite" -o -name "*.sqlite3" \) 2>/dev/null)
    done

    echo "$db_count"
}

# Restore databases from a snapshot's db backup
restore_databases() {
    local db_dir="$1"
    [[ -d "$db_dir" ]] || return

    find "$db_dir" -type f \( -name "*.db" -o -name "*.sqlite" -o -name "*.sqlite3" \) | while IFS= read -r db_backup; do
        local rel_path="${db_backup#$db_dir/}"
        local dest="$SCRIPT_DIR/$rel_path"
        mkdir -p "$(dirname "$dest")"
        # Use sqlite3 .backup to restore safely
        if command -v sqlite3 &>/dev/null && [[ -f "$dest" ]]; then
            # Restore into existing db without corrupting
            sqlite3 "$dest" ".restore '$db_backup'" 2>/dev/null || cp "$db_backup" "$dest"
        else
            cp "$db_backup" "$dest"
        fi
    done
}

get_snapshot_index() {
    local index_file="$SNAPSHOT_DIR/index.txt"
    [[ -f "$index_file" ]] || touch "$index_file"
    echo "$index_file"
}

next_id() {
    local index_file
    index_file="$(get_snapshot_index)"
    local last_id
    last_id=$(tail -1 "$index_file" 2>/dev/null | cut -d'|' -f1 || echo 0)
    echo $(( ${last_id:-0} + 1 ))
}

format_size() {
    local bytes=$1
    if (( bytes >= 1073741824 )); then
        printf "%.1f GB" "$(echo "$bytes / 1073741824" | bc -l)"
    elif (( bytes >= 1048576 )); then
        printf "%.1f MB" "$(echo "$bytes / 1048576" | bc -l)"
    elif (( bytes >= 1024 )); then
        printf "%.1f KB" "$(echo "$bytes / 1024" | bc -l)"
    else
        printf "%d B" "$bytes"
    fi
}

# ── Commands ─────────────────────────────────────

cmd_save() {
    local site=""
    local message=""

    # Parse args
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -m|--message) message="$2"; shift 2 ;;
            *) site="$1"; shift ;;
        esac
    done

    local id
    id="$(next_id)"
    local timestamp
    timestamp="$(date '+%Y-%m-%d_%H-%M-%S')"
    local human_date
    human_date="$(date '+%a %b %d %Y, %H:%M:%S')"
    local archive_name

    # Safe database backup into isolated temp dir
    local target="${site:-all}"
    local db_tmp
    db_tmp=$(mktemp -d)
    local db_staging="$db_tmp/_databases"
    echo -e "${CYAN}Backing up databases safely (no server impact)...${NC}"
    local db_count
    db_count=$(backup_databases "$db_staging" "$target")
    echo -e "  ${GREEN}$db_count database(s) backed up via sqlite3 .backup${NC}"

    if [[ -n "$site" ]]; then
        # Single site
        if [[ ! -d "$SCRIPT_DIR/$site" ]]; then
            echo -e "${RED}Error:${NC} Site '$site' not found."
            echo "Available sites:"
            for s in "${SITES[@]}"; do
                [[ -d "$SCRIPT_DIR/$s" ]] && echo "  $s"
            done
            rm -rf "$db_tmp"
            exit 1
        fi
        archive_name="${id}_${site}_${timestamp}.tar.gz"
        echo -e "${CYAN}Saving snapshot of ${BOLD}$site${NC}${CYAN}...${NC}"
        tar -czf "$SNAPSHOT_DIR/$archive_name" \
            $(build_exclude_args) \
            -C "$SCRIPT_DIR" "$site" \
            -C "$db_tmp" "_databases"
    else
        # All sites
        archive_name="${id}_all_${timestamp}.tar.gz"
        echo -e "${CYAN}Saving snapshot of ${BOLD}all sites${NC}${CYAN}...${NC}"
        local dirs=()
        for s in "${SITES[@]}"; do
            [[ -d "$SCRIPT_DIR/$s" ]] && dirs+=("$s")
        done
        tar -czf "$SNAPSHOT_DIR/$archive_name" \
            $(build_exclude_args) \
            -C "$SCRIPT_DIR" "${dirs[@]}" \
            -C "$db_tmp" "_databases"
    fi

    # Clean up temp dir
    rm -rf "$db_tmp"

    local size
    size=$(stat -f%z "$SNAPSHOT_DIR/$archive_name" 2>/dev/null || stat -c%s "$SNAPSHOT_DIR/$archive_name" 2>/dev/null)
    local size_fmt
    size_fmt="$(format_size "$size")"

    # Record in index (now includes human-readable date)
    echo "${id}|${timestamp}|${target}|${archive_name}|${size}|${message}|${human_date}" >> "$(get_snapshot_index)"

    echo -e "${GREEN}Snapshot #${id} saved${NC} ($size_fmt)"
    echo -e "  Taken: ${BOLD}${human_date}${NC}"
    [[ -n "$message" ]] && echo -e "  Note:  ${YELLOW}${message}${NC}"
    echo -e "  Restore with: ${BOLD}./snapshot.sh restore $id${NC}"
}

cmd_list() {
    local filter_site="${1:-}"
    local index_file
    index_file="$(get_snapshot_index)"

    if [[ ! -s "$index_file" ]]; then
        echo -e "${YELLOW}No snapshots yet.${NC} Run ${BOLD}./snapshot.sh save${NC} to create one."
        return
    fi

    echo ""
    printf "${BOLD}%-4s  %-28s  %-12s  %-10s  %s${NC}\n" "ID" "Date & Time" "Site" "Size" "Note"
    printf "%-4s  %-28s  %-12s  %-10s  %s\n" "───" "────────────────────────────" "────────────" "──────────" "────────────────"

    while IFS='|' read -r id timestamp target archive size message human_date; do
        [[ -n "$filter_site" && "$target" != "$filter_site" ]] && continue
        [[ ! -f "$SNAPSHOT_DIR/$archive" ]] && continue

        local size_fmt
        size_fmt="$(format_size "$size")"

        # Use human-readable date if available, otherwise format from timestamp
        local date_display
        if [[ -n "$human_date" ]]; then
            date_display="$human_date"
        else
            date_display="$(echo "$timestamp" | sed 's/_/ /;s/-/:/4;s/-/:/4')"
        fi

        printf "%-4s  %-28s  %-12s  %-10s  %s\n" "$id" "$date_display" "$target" "$size_fmt" "$message"
    done < "$index_file"
    echo ""
}

cmd_restore() {
    local target_id="${1:-}"
    if [[ -z "$target_id" ]]; then
        echo -e "${RED}Usage:${NC} ./snapshot.sh restore <id>"
        echo "Run ${BOLD}./snapshot.sh list${NC} to see available snapshots."
        exit 1
    fi

    local index_file
    index_file="$(get_snapshot_index)"
    local line
    line=$(grep "^${target_id}|" "$index_file" 2>/dev/null || true)

    if [[ -z "$line" ]]; then
        echo -e "${RED}Error:${NC} Snapshot #$target_id not found."
        exit 1
    fi

    local timestamp target archive message
    IFS='|' read -r _ timestamp target archive _ message <<< "$line"

    if [[ ! -f "$SNAPSHOT_DIR/$archive" ]]; then
        echo -e "${RED}Error:${NC} Archive file missing: $archive"
        exit 1
    fi

    local date_fmt
    date_fmt="$(echo "$timestamp" | sed 's/_/ /;s/-/:/4;s/-/:/4')"

    echo ""
    echo -e "${YELLOW}About to restore snapshot #$target_id${NC}"
    echo -e "  Date:  $date_fmt"
    echo -e "  Site:  $target"
    [[ -n "$message" ]] && echo -e "  Note:  $message"
    echo ""

    # Auto-save current state before restoring
    echo -e "${CYAN}Auto-saving current state before restore...${NC}"
    if [[ "$target" == "all" ]]; then
        cmd_save -m "auto-save before restoring #$target_id"
    else
        cmd_save "$target" -m "auto-save before restoring #$target_id"
    fi

    echo ""
    echo -e "${CYAN}Restoring code files...${NC}"

    if [[ "$target" == "all" ]]; then
        for s in "${SITES[@]}"; do
            [[ -d "$SCRIPT_DIR/$s" ]] && rm -rf "$SCRIPT_DIR/$s"
        done
    else
        rm -rf "$SCRIPT_DIR/$target"
    fi

    # Extract to a temp dir first so we can handle databases separately
    local restore_tmp
    restore_tmp=$(mktemp -d)
    tar -xzf "$SNAPSHOT_DIR/$archive" -C "$restore_tmp"

    # Move code files back
    if [[ "$target" == "all" ]]; then
        for s in "${SITES[@]}"; do
            [[ -d "$restore_tmp/$s" ]] && mv "$restore_tmp/$s" "$SCRIPT_DIR/$s"
        done
    else
        [[ -d "$restore_tmp/$target" ]] && mv "$restore_tmp/$target" "$SCRIPT_DIR/$target"
    fi

    # Restore databases safely
    if [[ -d "$restore_tmp/_databases" ]]; then
        echo -e "${CYAN}Restoring databases safely...${NC}"
        restore_databases "$restore_tmp/_databases"
        echo -e "  ${GREEN}Databases restored${NC}"
    fi

    rm -rf "$restore_tmp"

    echo -e "${GREEN}Restored snapshot #$target_id${NC}"
    echo -e "  ${YELLOW}Note:${NC} You may need to restart your services for changes to take effect."
}

cmd_diff() {
    local target_id="${1:-}"
    if [[ -z "$target_id" ]]; then
        echo -e "${RED}Usage:${NC} ./snapshot.sh diff <id>"
        exit 1
    fi

    local index_file
    index_file="$(get_snapshot_index)"
    local line
    line=$(grep "^${target_id}|" "$index_file" 2>/dev/null || true)

    if [[ -z "$line" ]]; then
        echo -e "${RED}Error:${NC} Snapshot #$target_id not found."
        exit 1
    fi

    local archive target
    IFS='|' read -r _ _ target archive _ _ <<< "$line"

    if [[ ! -f "$SNAPSHOT_DIR/$archive" ]]; then
        echo -e "${RED}Error:${NC} Archive file missing."
        exit 1
    fi

    local tmp_dir
    tmp_dir=$(mktemp -d)
    trap "rm -rf '$tmp_dir'" EXIT

    tar -xzf "$SNAPSHOT_DIR/$archive" -C "$tmp_dir"

    echo -e "${BOLD}Changes since snapshot #$target_id:${NC}"
    echo ""

    if [[ "$target" == "all" ]]; then
        for s in "${SITES[@]}"; do
            [[ -d "$SCRIPT_DIR/$s" ]] || continue
            local result
            result=$(diff -rq "$tmp_dir/$s" "$SCRIPT_DIR/$s" 2>/dev/null || true)
            if [[ -n "$result" ]]; then
                echo -e "${BLUE}── $s ──${NC}"
                echo "$result" | sed 's|'"$tmp_dir"'/|snapshot:|g; s|'"$SCRIPT_DIR"'/||g'
                echo ""
            fi
        done
    else
        diff -rq "$tmp_dir/$target" "$SCRIPT_DIR/$target" 2>/dev/null \
            | sed 's|'"$tmp_dir"'/|snapshot:|g; s|'"$SCRIPT_DIR"'/||g' \
            || echo -e "${GREEN}No changes.${NC}"
    fi
}

cmd_delete() {
    local target_id="${1:-}"
    if [[ -z "$target_id" ]]; then
        echo -e "${RED}Usage:${NC} ./snapshot.sh delete <id>"
        exit 1
    fi

    local index_file
    index_file="$(get_snapshot_index)"
    local line
    line=$(grep "^${target_id}|" "$index_file" 2>/dev/null || true)

    if [[ -z "$line" ]]; then
        echo -e "${RED}Error:${NC} Snapshot #$target_id not found."
        exit 1
    fi

    local archive
    IFS='|' read -r _ _ _ archive _ _ <<< "$line"

    rm -f "$SNAPSHOT_DIR/$archive"
    # Remove line from index (portable sed)
    grep -v "^${target_id}|" "$index_file" > "${index_file}.tmp" || true
    mv "${index_file}.tmp" "$index_file"

    echo -e "${GREEN}Deleted snapshot #$target_id${NC}"
}

cmd_prune() {
    local keep="${1:-10}"
    local index_file
    index_file="$(get_snapshot_index)"

    if [[ ! -s "$index_file" ]]; then
        echo "No snapshots to prune."
        return
    fi

    local deleted=0

    # Get unique sites
    local sites_in_index
    sites_in_index=$(cut -d'|' -f3 "$index_file" | sort -u)

    for site in $sites_in_index; do
        local count
        count=$(grep "|${site}|" "$index_file" | wc -l | tr -d ' ')
        if (( count > keep )); then
            local to_delete=$(( count - keep ))
            grep "|${site}|" "$index_file" | head -n "$to_delete" | while IFS='|' read -r id _ _ archive _ _; do
                rm -f "$SNAPSHOT_DIR/$archive"
                deleted=$(( deleted + 1 ))
            done
            # Keep only the last N entries for this site
            local tmp
            tmp=$(mktemp)
            grep -v "|${site}|" "$index_file" > "$tmp" || true
            grep "|${site}|" "$index_file" | tail -n "$keep" >> "$tmp"
            sort -t'|' -k1 -n "$tmp" > "$index_file"
            rm -f "$tmp"
        fi
    done

    echo -e "${GREEN}Pruned old snapshots. Keeping last $keep per site.${NC}"

    local total_size=0
    for f in "$SNAPSHOT_DIR"/*.tar.gz; do
        [[ -f "$f" ]] || continue
        local s
        s=$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null)
        total_size=$(( total_size + s ))
    done
    echo -e "Total snapshot storage: $(format_size $total_size)"
}

# ── Main ─────────────────────────────────────────

case "${1:-help}" in
    save)    shift; cmd_save "$@" ;;
    list|ls) shift; cmd_list "$@" ;;
    restore) shift; cmd_restore "$@" ;;
    diff)    shift; cmd_diff "$@" ;;
    delete|rm) shift; cmd_delete "$@" ;;
    prune)   shift; cmd_prune "$@" ;;
    help|--help|-h)
        echo ""
        echo -e "${BOLD}snapshot.sh${NC} — Save and restore website states"
        echo ""
        echo -e "${BOLD}Commands:${NC}"
        echo "  save [site] [-m \"note\"]   Save current state (all sites or one)"
        echo "  list [site]               List all snapshots"
        echo "  restore <id>              Restore a snapshot (auto-saves current state first)"
        echo "  diff <id>                 Show what changed since a snapshot"
        echo "  delete <id>               Delete a snapshot"
        echo "  prune [n]                 Keep only last n snapshots per site (default: 10)"
        echo ""
        echo -e "${BOLD}Sites:${NC}"
        for s in "${SITES[@]}"; do
            if [[ -d "$SCRIPT_DIR/$s" ]]; then
                echo -e "  ${GREEN}$s${NC}"
            else
                echo -e "  ${RED}$s${NC} (not found)"
            fi
        done
        echo ""
        echo -e "${BOLD}Examples:${NC}"
        echo "  ./snapshot.sh save                          # save everything"
        echo "  ./snapshot.sh save gateway -m \"before CSS\"  # save gateway with note"
        echo "  ./snapshot.sh list                          # see all snapshots"
        echo "  ./snapshot.sh restore 3                     # go back to snapshot #3"
        echo "  ./snapshot.sh diff 3                        # see changes vs #3"
        echo ""
        ;;
    *)
        echo -e "${RED}Unknown command:${NC} $1"
        echo "Run ${BOLD}./snapshot.sh help${NC} for usage."
        exit 1
        ;;
esac
