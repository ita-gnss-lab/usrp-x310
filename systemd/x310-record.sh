#!/usr/bin/env bash
# Wrapper invoked by x310-record.timer.
#
# Enforces, independently of the timer schedule:
#   1. at most $MAX_PER_DAY recordings per calendar day
#   2. only inside the [$WINDOW_START, $WINDOW_END) local-time window
#
# The timer is set to fire at 16:00 and 17:00, which already satisfies both
# rules by construction -- but this script re-checks both anyway, so a
# manual `systemctl start`, a Persistent=true catch-up run after the host
# was off during a scheduled slot, or a future timer edit can never record
# outside the window or push the daily count past the limit.

set -euo pipefail

REPO_DIR="/home/tapyu/git/usrp-x310"
UV_BIN="/home/tapyu/.local/bin/uv"
# STATE_DIR is just a path the script writes its own bookkeeping files into
# The ${STATE_DIRECTORY:-/var/lib/x310-recorder} syntax picks $STATE_DIRECTORY if it's set, else falls back to /var/lib/x310-recorder. 
# $STATE_DIRECTORY isn't something I invented — it's an environment variable systemd itself injects because x310-record.service has StateDirectory=x310-recorder: systemd creates /var/lib/x310-recorder, chowns it to the service's User=tapyu, and passes its path (/var/lib/x310-recorder) into the service's environment as $STATE_DIRECTORY. The /var/lib/x310-recorder fallback only matters if you ever run the script by hand outside systemd (e.g. to test it), where that env var wouldn't be set.
STATE_DIR="${STATE_DIRECTORY:-/var/lib/x310-recorder}"

WINDOW_START="16:00"
WINDOW_END="18:00"
MAX_PER_DAY=2

mkdir -p "$STATE_DIR"

# Refuse to overlap with another run of this script (manual trigger racing the timer, etc).
exec 9>"$STATE_DIR/.lock"
flock -n 9 || { echo "another run is already in progress; skipping"; exit 0; }

now_hm=$(date +%H:%M)
if [[ "$now_hm" < "$WINDOW_START" || "$now_hm" > "$WINDOW_END" ]]; then
    echo "now=$now_hm is outside the allowed [$WINDOW_START, $WINDOW_END) window; skipping"
    exit 0
fi

today=$(date +%F)
count_file="$STATE_DIR/count-$today"
count=0
[[ -f "$count_file" ]] && count=$(<"$count_file")

if (( count >= MAX_PER_DAY )); then
    echo "already recorded $count time(s) today ($today), limit is $MAX_PER_DAY; skipping"
    exit 0
fi

cd "$REPO_DIR"
name="x310_$(date -u +%Y%m%dT%H%M%SZ)"
echo "recording ($((count + 1))/$MAX_PER_DAY today) as '$name' ..."
"$UV_BIN" run main.py --name "$name"

echo $((count + 1)) > "$count_file"
# Clean up old count files, so the state directory doesn't grow unbounded.
find "$STATE_DIR" -maxdepth 1 -name 'count-*' -mtime +7 -delete
