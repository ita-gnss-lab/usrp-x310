#!/usr/bin/env bash
# Wrapper invoked by x310-record.timer every 15 min between 16:00 and 18:00.
#
# Per report/main.tex's recording guidelines:
#   1. only inside the [$WINDOW_START, $WINDOW_END) local-time window
#   2. senses whether the USRP is reachable via `uhd_find_devices`; if not,
#      exits silently and is sensed again at the next 15-min tick
#   3. once found, records (each recording is x310usrp_record.py's default: 5 min)
#   4. at most $MAX_PER_DAY recordings per calendar day
#   5. the second recording must start at least $MIN_GAP_SEC after the first
#   6. records to a local staging directory, then moves the finished archive to
#      the NAS -- writing straight to the NAS (SMB) stalls the receive loop for
#      ~100 ms at a time, longer than the L5 receive buffer lasts, so samples drop
#
# All five are re-checked here regardless of the timer schedule, so a manual
# `systemctl start`, a Persistent=true catch-up run, or a future timer edit
# can never record outside the window, more than twice a day, or less than
# 30 min apart.

set -euo pipefail

REPO_DIR="/home/tapyu/git/usrp-x310"
UV_BIN="/home/tapyu/.local/bin/uv"
STATE_DIR="${STATE_DIRECTORY:-/var/lib/x310-recorder}"
USRP_ARGS="addr=192.168.10.2"
WINDOW_START="16:00"
WINDOW_END="18:00"
MAX_PER_DAY=2
MIN_GAP_SEC=$((30 * 60))
# Record to the local disk first (fast, steady writes), then move the archive to the NAS.
STAGING_DIR="$REPO_DIR/staging"
NAS_MOUNT="/mnt/nas"
NAS_DIR="$NAS_MOUNT/tapyu/recordings_usrp_x310_sjc"  # what $REPO_DIR/recordings links to
MIN_FREE_GB=35  # one 8-bit recording (~14.4 GB) + its archive copy while bundling (~14.4 GB) + margin

# Move every finished archive from staging to the NAS. rsync writes each file under a hidden
# temporary name and renames it only once complete (so the NAS never shows a truncated .sigmf),
# checksums the transfer, and deletes the local copy only after it succeeded. -rt rather than -a:
# the SMB share sets its own owner/permissions, so only files + timestamps are copied.
# Never fatal: whatever is left in staging is retried on the next run.
move_to_nas() {
    if ! mountpoint -q "$NAS_MOUNT"; then
        echo "warning: NAS not mounted at $NAS_MOUNT; keeping archives in $STAGING_DIR for now"
        return 0
    fi
    local files=("$STAGING_DIR"/*.sigmf)
    [[ -e "${files[0]}" ]] || return 0
    if rsync -rt --remove-source-files "${files[@]}" "$NAS_DIR/"; then
        echo "moved ${#files[@]} archive(s) to $NAS_DIR"
    else
        echo "warning: rsync to $NAS_DIR failed; whatever it didn't move stays in $STAGING_DIR"
    fi
}

mkdir -p "$STATE_DIR"

# Refuse to overlap with another run of this script (manual trigger racing the timer, etc).
exec 9>"$STATE_DIR/.lock"
# flock locks a file so only one process can hold it at a time — a mutex, using the filesystem instead of memory. The script opens .lock and calls flock -n 9 on it; if another instance already holds it, that call fails immediately instead of waiting, and the script exits instead of running a second recording in parallel. The lock is automatically released when the process that holds it exits, crash or not — no manual unlock needed.
flock -n 9 || { echo "another run is already in progress; skipping"; exit 0; }

now_hm=$(date +%H:%M)
if [[ "$now_hm" < "$WINDOW_START" || "$now_hm" > "$WINDOW_END" ]]; then
    echo "now=$now_hm is outside the allowed [$WINDOW_START, $WINDOW_END) window; skipping"
    exit 0
fi

# One line per completed recording today, each the epoch second it started.
day_log="$STATE_DIR/day-$(date +%F).log" # NOTE: %F is shorthand for %Y-%m-%d — date +%F prints today's date as 2026-10-08.
touch "$day_log"
mapfile -t today_runs < "$day_log" # NOTE: mapfile -t reads the file line by line into a bash array — here, today_runs gets one element per line of day_log, each being an epoch-second timestamp from a past recording today. -t strips the trailing newline from each line so the array holds clean values instead of "1696789200\n".

# If more than $MAX_PER_DAY recordings have already been done today, skip this run.
count=${#today_runs[@]} # number of recordings already done today
if (( count >= MAX_PER_DAY )); then
    echo "already recorded $count time(s) today, limit is $MAX_PER_DAY; skipping"
    exit 0
fi

now_epoch=$(date +%s) # date +%s prints the number of seconds since 1970-01-01 UTC, i.e. "now" as a single integer instead of a formatted date string.
if (( count == 1 )); then
    gap_sec=$((now_epoch - today_runs[0])) # compute the difference in seconds between now and the first recording today
    if (( gap_sec < MIN_GAP_SEC )); then
        echo "only ${gap_sec}s since today's first recording, need, at minimum, ${MIN_GAP_SEC}s; skipping"
        exit 0
    fi
fi

# sense and skip if the USRP is not reachable
if ! uhd_find_devices --args="$USRP_ARGS" >/dev/null 2>&1; then
    echo "USRP not detected at $USRP_ARGS; will sense again in ~15 min"
    exit 0
fi

# skip (instead of failing mid-recording) if the local disk can't hold a recording
mkdir -p "$STAGING_DIR"
free_gb=$(( $(df --output=avail -k "$STAGING_DIR" | tail -1) / 1024 / 1024 ))
if (( free_gb < MIN_FREE_GB )); then
    echo "only ${free_gb} GB free in $STAGING_DIR, need ${MIN_FREE_GB} GB; skipping"
    move_to_nas  # maybe frees space for the next tick
    exit 0
fi

# Record during 5 min
cd "$REPO_DIR"
name="x310_$(date -u +%Y%m%dT%H%M%SZ)" # builds a UTC timestamp string and prefixes it with x310_, e.g. x310_20261008T161530Z (year, month, day, literal T, hour, minute, second, literal Z (for Zulu/UTC), via -u to force UTC regardless of the system's local timezone).
echo "USRP detected; recording ($((count + 1))/$MAX_PER_DAY today) as '$name' ..."
"$UV_BIN" run x310usrp_record.py --name "$name" --args "$USRP_ARGS" --out-dir "$STAGING_DIR"

# append the timestamp of this recording to the per-day log
echo "$now_epoch" >> "$day_log"
# Clean up old per-day logs, so the state directory doesn't grow unbounded.
find "$STATE_DIR" -maxdepth 1 -name 'day-*.log' -mtime +30 -delete

# the recording is done (and counted), so the slow NAS copy can't cost samples anymore
move_to_nas
