#!/usr/bin/env bash
# Wrapper invoked by x310-record.timer every 15 min between 21:00 and 23:00.
#
# Per report/main.tex's recording guidelines:
#   1. only inside the [$WINDOW_START, $WINDOW_END) local-time window
#   2. senses whether the USRP is reachable via `uhd_find_devices`; if not,
#      exits silently and is sensed again at the next 15-min tick
#   3. once found, records (each recording is x310usrp_record.py's default: 5 min), with the profile of the day's
#      recording number (PROFILES below): the 1st is GPS L1 C/A (4 MS/s) + L5 (20 MS/s), 8-bit; the 2nd is
#      GPS L1 C/A + L2C, both 4 MS/s, 16-bit
#   4. at most $MAX_PER_DAY recordings per calendar day
#   5. the second recording must start at least $MIN_GAP_SEC after the first
#   6. records to a local staging directory, then bundles the finished recording
#      straight into a .sigmf archive on the NAS (--out-dir), so the local disk
#      only ever holds one copy -- recording straight to the NAS (SMB) would stall
#      the receive loop for ~100 ms at a time, longer than the L5 receive buffer
#      lasts, so samples would drop; bundling happens after recording, when that
#      no longer matters. If the NAS is not mounted, the archive is built in
#      staging and moved by move_to_nas on a later run.
#   7. processes the new recording with GNSS-SDR (run_gnss_sdr.sh --report, with the profile's config, plus a second
#      Galileo E1 + E5a run for the 1st recording) and turns every receiver stage's outputs into figures/tables in
#      report/gnss_sdr/<name>/ (gnss_sdr_out2tex.py)
#   8. adds the recording (metadata table + those GNSS-SDR results) to report/main.tex and recompiles the PDF
#      (update_report.py); the 15-day x310-report.service only sends that PDF by WhatsApp
#   9. commits the report (report/main.tex, main.pdf, report/gnss_sdr/) and pushes it to GitHub (push_report)
#   Steps 7-9 are never fatal: a failure leaves the recording itself untouched and is only logged
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
WINDOW_START="21:00"
WINDOW_END="23:00"
MAX_PER_DAY=2
MIN_GAP_SEC=$((30 * 60))
# Record to the local disk first (fast, steady writes), then bundle the archive straight onto the NAS.
STAGING_DIR="$REPO_DIR/staging"
NAS_MOUNT="/mnt/nas"
NAS_DIR="$NAS_MOUNT/tapyu/recordings_usrp_x310_sjc"  # what $REPO_DIR/recordings links to
# the larger profile (L1/L5 8-bit) is ~14.4 GB for 5 min: bundled on the NAS, staging only holds that one copy;
# bundled locally (NAS not mounted), it briefly holds it twice
MIN_FREE_GB_NAS=20
MIN_FREE_GB_LOCAL=35
# Recording profile per recording of the day (index = recordings already made today): x310usrp_record.py options,
# then the gnss-sdr config that processes it.
#   0: GPS L1 C/A + L5, 8-bit (ci8): the default --freq/--iq-rate (L1 4 MS/s, L5 20 MS/s), --int8-div 1 3 puts the
#      noise std at ~30 of 127 at 25 dB gain (RECOMMENDED_INT8_DIVS in x310usrp_record.py)
#   1: GPS L1 C/A + L2C (1227.60 MHz), both 4 MS/s, 16-bit (ci16_le, no --int8-div): small enough at 4 MS/s
#      (9.6 GB), and no divisor to tune
PROFILES=("--freq 1575.42e6 1176.45e6 --gain 25 25 --int8-div 1 3"
          "--freq 1575.42e6 1227.6e6 --gain 25 25")
PROFILE_CONFS=("l1ca8bits4msps_l5_8bits20msps.conf" "l1ca16bits4msps_l2c16bits4msps.conf")
PROFILE_NAMES=("GPS L1 C/A + L5, 8-bit" "GPS L1 C/A + L2C, 16-bit")
# a second gnss-sdr run of the same recording ("" = none): the 1st recording's L1/L5 bands also hold Galileo E1/E5a,
# whose dual-frequency (Iono-Free-LC) position gnss-sdr computes itself; the 2nd profile's config tracks Galileo E1
# beside GPS in its single run
PROFILE_EXTRA_CONFS=("e1_8bits4msps_e5a_8bits20msps.conf" "")
PROFILE_EXTRA_TAGS=("galileo" "")
REPORT_DIR="$REPO_DIR/report/gnss_sdr"
# pushed over SSH with ~/.ssh/id_ed25519 (registered on GitHub, no passphrase): origin is HTTPS, whose credential
# helper (Git Credential Manager, gpg store) needs a terminal to unlock the token, which a systemd service lacks
GIT_PUSH_URL="git@github.com:ita-gnss-lab/usrp-x310.git"
REPORT_PATHS=(report/main.tex report/main.pdf report/gnss_sdr)

# Process recording $1 (archive path) with gnss-sdr config $2, write its report figures/tables, and drop the bulky
# per-stage dumps afterwards (tracking/telemetry dumps are ~1 GB per run; the figures keep what they showed).
# Optional $3 (tag): a further run of the same recording, to gnss_sdr_output/<name>_<tag>/ and
# report/gnss_sdr/<name>/results_<tag>.tex, with gnss-sdr's own PVT as configured (no --l1-pvt).
process_recording() {
    local archive=$1 conf=$2 tag=${3:-} name run log
    local -a run_args=(--l1-pvt) tex_args=()
    name=$(basename "$archive" .sigmf)
    run=$name
    if [[ -n "$tag" ]]; then
        run=${name}_$tag
        run_args=(--out-suffix "_$tag")
        tex_args=(--tag "$tag")
    fi
    log="$STATE_DIR/gnss-sdr-$run.log"
    echo "processing $name with GNSS-SDR ($conf) ..."
    "$REPO_DIR/run_gnss_sdr.sh" --conf "$REPO_DIR/$conf" --report "${run_args[@]}" "$archive" > "$log" 2>&1 \
        || { echo "warning: GNSS-SDR processing of $name ($conf) failed; see $log"; return 0; }
    "$UV_BIN" run gnss_sdr_out2tex.py "$REPO_DIR/gnss_sdr_output/$run" --recording "$archive" \
        --out "$REPORT_DIR/$name" --tex-root "$REPO_DIR/report" "${tex_args[@]}" \
        || { echo "warning: report figures of $run failed"; return 0; }
    find "$REPO_DIR/gnss_sdr_output/$run" -maxdepth 1 \( -name 'trk_*' -o -name 'tlm_*' -o -name 'acq_*.mat' \
        -o -name '*.dat' \) -delete
    echo "GNSS-SDR results of $run in $REPORT_DIR/$name"
}

# Commit the report files the processing changed (only REPORT_PATHS: whatever else is modified or staged in the
# working tree, e.g. code being edited, stays out of the commit) and push the checked-out branch to GitHub.
push_report() {
    local name=$1
    git -C "$REPO_DIR" add -A -- "${REPORT_PATHS[@]}" || { echo "warning: git add of the report failed"; return 0; }
    if git -C "$REPO_DIR" diff --cached --quiet -- "${REPORT_PATHS[@]}"; then
        echo "report unchanged; nothing to commit"
        return 0
    fi
    git -C "$REPO_DIR" commit --quiet -m "report: add recording $name" -- "${REPORT_PATHS[@]}" \
        || { echo "warning: committing the report failed"; return 0; }
    GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="ssh -o BatchMode=yes" \
        git -C "$REPO_DIR" push --quiet "$GIT_PUSH_URL" HEAD \
        || { echo "warning: pushing the report failed (committed locally; push by hand)"; return 0; }
    echo "report committed and pushed ($(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD))"
}

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

# where to bundle the archive: straight onto the NAS if it is mounted, otherwise locally
if mountpoint -q "$NAS_MOUNT"; then
    archive_args=(--out-dir "$NAS_DIR")
    MIN_FREE_GB=$MIN_FREE_GB_NAS
else
    echo "warning: NAS not mounted at $NAS_MOUNT; the archive will be built in $STAGING_DIR"
    archive_args=()
    MIN_FREE_GB=$MIN_FREE_GB_LOCAL
fi

# skip (instead of failing mid-recording) if the local disk can't hold a recording
mkdir -p "$STAGING_DIR"
free_gb=$(( $(df --output=avail -k "$STAGING_DIR" | tail -1) / 1024 / 1024 ))
if (( free_gb < MIN_FREE_GB )); then
    echo "only ${free_gb} GB free in $STAGING_DIR, need ${MIN_FREE_GB} GB; skipping"
    move_to_nas  # maybe frees space for the next tick
    exit 0
fi

# Record during 5 min, with today's profile
cd "$REPO_DIR"
name="x310_$(date -u +%Y%m%dT%H%M%SZ)" # builds a UTC timestamp string and prefixes it with x310_, e.g. x310_20261008T161530Z (year, month, day, literal T, hour, minute, second, literal Z (for Zulu/UTC), via -u to force UTC regardless of the system's local timezone).
read -ra profile_args <<< "${PROFILES[count]}"
echo "USRP detected; recording ($((count + 1))/$MAX_PER_DAY today, ${PROFILE_NAMES[count]}) as '$name' ..."
"$UV_BIN" run x310usrp_record.py --name "$name" --args "$USRP_ARGS" --staging-dir "$STAGING_DIR" \
    "${profile_args[@]}" "${archive_args[@]}"

# append the timestamp of this recording to the per-day log
echo "$now_epoch" >> "$day_log"
# Clean up old per-day logs, so the state directory doesn't grow unbounded.
find "$STATE_DIR" -maxdepth 1 -name 'day-*.log' -mtime +30 -delete

# archives built locally on earlier runs (NAS down at the time) are moved over now
move_to_nas

# 7. process it with GNSS-SDR (wherever the archive ended up: the NAS, or staging if it was not mounted)
archive="$NAS_DIR/$name.sigmf"
[[ -f "$archive" ]] || archive="$STAGING_DIR/$name.sigmf"
if [[ -f "$archive" ]]; then
    process_recording "$archive" "${PROFILE_CONFS[count]}"
    if [[ -n "${PROFILE_EXTRA_CONFS[count]}" ]]; then
        process_recording "$archive" "${PROFILE_EXTRA_CONFS[count]}" "${PROFILE_EXTRA_TAGS[count]}"
    fi
    # 8. add it to the report and recompile (with its GNSS-SDR results if step 7 produced them)
    "$UV_BIN" run update_report.py "$archive" --recordings-dir "$NAS_DIR" --tex-path "$REPO_DIR/report/main.tex" \
        || echo "warning: adding $name to the report failed"
    # 9. commit and push the updated report
    push_report "$name"
else
    echo "warning: archive of $name not found; not processed"
fi
