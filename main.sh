#!/bin/bash
#
# onion_crawler.sh
#
# Crawls seed URLs (clearnet or .onion) and extracts .onion links from the
# fetched pages, then checks which v3 addresses are alive.
#
# WHONIX NOTE: no proxy flags are used anywhere in this script. On Whonix
# Workstation, the firewall transparently forces all outbound TCP through
# the Gateway to Tor at the network level -- plain curl with no proxy
# config is already Tor-routed, and DNS is transparently resolved through
# Tor too. (This is intentional: it protects against an app leaking due
# to misconfiguration.) If you ever run this OUTSIDE Whonix, you would
# need to add --socks5-hostname 127.0.0.1:9050 back into the curl calls,
# or traffic will go out unproxied.
#
# Requires only: curl and standard coreutils (grep, sed, sort). No pip,
# no Python packages.
#
# IMPORTANT:
#   v2 onion addresses (16-char) are DEAD -- Tor removed v2 support from
#   the network in October 2021. This script still extracts v2-looking
#   strings into a separate file for cataloging, but never claims they
#   are "alive" because nothing can answer that protocol anymore.
#
# Usage:
#   ./onion_crawler.sh seeds.txt
#
# Output:
#   v3_alive.txt     - v3 onion hosts that responded
#   v3_dead.txt      - v3 onion hosts found but unreachable
#   v2_found.txt     - v2-looking strings found (unsupported, not checked)
#   crawl.log        - full run log

set -uo pipefail

# ---- config -----------------------------------------------------------
USER_AGENT="Mozilla/5.0 (Windows NT 10.0; rv:102.0) Gecko/20100101 Firefox/102.0"
CURL_TIMEOUT=25
CRAWL_DELAY=1        # seconds between requests
MAX_PARALLEL_CHECKS=6

SEEDS_FILE="${1:-seeds.txt}"
WORKDIR="$(pwd)"
RAW_PAGES_DIR="${WORKDIR}/.crawl_pages"
LOGFILE="${WORKDIR}/crawl.log"

V3_ALIVE="${WORKDIR}/v3_alive.txt"
V3_DEAD="${WORKDIR}/v3_dead.txt"
V2_FOUND="${WORKDIR}/v2_found.txt"

# Regexes (extended grep -E, case-insensitive via -i)
V3_REGEX='[a-z2-7]{56}\.onion'
V2_REGEX='[a-z2-7]{16}\.onion'

# ---- helpers ------------------------------------------------------------
log() {
    echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOGFILE" >&2
}

check_tor() {
    log "Checking Tor connectivity (via Whonix transparent proxy)..."
    local resp
    resp=$(curl -s --max-time "$CURL_TIMEOUT" -A "$USER_AGENT" \
                "https://check.torproject.org/" 2>>"$LOGFILE")
    if echo "$resp" | grep -qi "Congratulations"; then
        log "OK: traffic confirmed routed through Tor."
    else
        log "ERROR: could not confirm Tor routing. On Whonix this usually means the Gateway isn't reachable or Tor isn't bootstrapped yet -- check Gateway status."
        exit 1
    fi
}

fetch() {
    # $1 = url, prints body to stdout, returns curl exit code
    curl -s -L --max-time "$CURL_TIMEOUT" -A "$USER_AGENT" "$1" 2>>"$LOGFILE"
}

extract_onions() {
    # $1 = file containing page text; prints unique v3 and v2 hosts
    grep -oiE "$V3_REGEX" "$1" 2>/dev/null | tr 'A-Z' 'a-z' | sort -u > "${1}.v3"
    grep -oiE "$V2_REGEX" "$1" 2>/dev/null | tr 'A-Z' 'a-z' | sort -u > "${1}.v2"
}

check_v3_alive() {
    # $1 = onion host (no scheme). Tries https then http.
    local host="$1"
    for scheme in https http; do
        local code
        code=$(curl -s -o /dev/null -w "%{http_code}" \
                    --max-time "$CURL_TIMEOUT" -A "$USER_AGENT" \
                    "${scheme}://${host}/" 2>>"$LOGFILE")
        # any non-zero HTTP status means something answered
        if [[ "$code" =~ ^[0-9]+$ ]] && [ "$code" != "000" ]; then
            echo "${host} ${scheme} ${code}"
            return 0
        fi
    done
    return 1
}

# ---- main ---------------------------------------------------------------
if [ ! -f "$SEEDS_FILE" ]; then
    echo "Seed file not found: $SEEDS_FILE" >&2
    echo "Usage: $0 seeds.txt" >&2
    exit 1
fi

: > "$LOGFILE"
: > "$V3_ALIVE"
: > "$V3_DEAD"
: > "$V2_FOUND"
mkdir -p "$RAW_PAGES_DIR"

check_tor

all_v3_file="${RAW_PAGES_DIR}/all_v3.txt"
all_v2_file="${RAW_PAGES_DIR}/all_v2.txt"
: > "$all_v3_file"
: > "$all_v2_file"

i=0
while IFS= read -r url; do
    [ -z "$url" ] && continue
    case "$url" in \#*) continue ;; esac

    i=$((i+1))
    page_file="${RAW_PAGES_DIR}/page_${i}.html"

    log "Fetching (${i}): $url"
    fetch "$url" > "$page_file"

    if [ ! -s "$page_file" ]; then
        log "  -> empty/failed response"
        sleep "$CRAWL_DELAY"
        continue
    fi

    extract_onions "$page_file"
    v3_count=$(wc -l < "${page_file}.v3")
    v2_count=$(wc -l < "${page_file}.v2")
    log "  -> found ${v3_count} v3 refs, ${v2_count} v2 refs"

    cat "${page_file}.v3" >> "$all_v3_file"
    cat "${page_file}.v2" >> "$all_v2_file"

    sleep "$CRAWL_DELAY"
done < "$SEEDS_FILE"

sort -u "$all_v3_file" -o "$all_v3_file"
sort -u "$all_v2_file" -o "$V2_FOUND"

total_v3=$(wc -l < "$all_v3_file")
log "Total unique v3 candidates: $total_v3"
log "Checking liveness (parallel, max ${MAX_PARALLEL_CHECKS})..."

export -f check_v3_alive
export CURL_TIMEOUT USER_AGENT LOGFILE

xargs -a "$all_v3_file" -P "$MAX_PARALLEL_CHECKS" -I{} bash -c '
    result=$(check_v3_alive "{}")
    if [ $? -eq 0 ]; then
        echo "$result"
    else
        echo "{}"  >> "'"$V3_DEAD"'"
    fi
' >> "$V3_ALIVE"

alive_count=$(wc -l < "$V3_ALIVE")
dead_count=$(wc -l < "$V3_DEAD")
v2_count=$(wc -l < "$V2_FOUND")

log "Done."
echo ""
echo "=== Results ==="
echo "Live v3 onions:        $alive_count  -> $V3_ALIVE"
echo "Unreachable v3 onions: $dead_count  -> $V3_DEAD"
echo "v2 strings found:      $v2_count  -> $V2_FOUND (unsupported by Tor since Oct 2021, not checked)"
echo "Full log:              $LOGFILE"

rm -rf "$RAW_PAGES_DIR"
