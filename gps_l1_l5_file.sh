#!/usr/bin/env bash
# Run GNSS-SDR (gps_l1_l5_file.conf) on one x310usrp_record.py recording.
#
# Usage:
#   ./gps_l1_l5_file.sh [recordings/<name>.sigmf]    # default: the newest archive in recordings/
#
# Steps:
#   1. checks the archive's metadata matches what gps_l1_l5_file.conf expects
#      (ci8 samples, L1 = ch0 at 4 MS/s, L5 = ch1 at 20 MS/s)
#   2. extracts the two .sigmf-data files to a temporary directory in /tmp
#   3. runs gnss-sdr with a copy of the config pointing at those files, inside
#      gnss_sdr_output/<name>/, so its outputs (PVT/RINEX/KML/...) and logs stay per recording
#   4. deletes the extracted files on exit -- also on an error or Ctrl+C

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="$REPO_DIR/gps_l1_l5_file.conf"

archive="${1:-$(ls -t "$REPO_DIR"/recordings/*.sigmf 2>/dev/null | head -1)}"
[[ -n "$archive" && -f "$archive" ]] || { echo "error: no .sigmf archive given or found in recordings/" >&2; exit 1; }
archive="$(realpath "$archive")"
name="$(basename "$archive" .sigmf)"
out_dir="$REPO_DIR/gnss_sdr_output/$name"
echo "recording: $archive"

# 1. the config hard-codes the sample format and rates, so refuse recordings that don't match
for ch_rate in "0 4000000" "1 20000000"; do
    read -r ch rate <<< "$ch_rate"
    tar -xOf "$archive" "$name/${name}_ch$ch.sigmf-meta" | python3 -c "
import json, sys
g = json.load(sys.stdin)['global']
if g['core:datatype'] != 'ci8' or g['core:sample_rate'] != $rate:
    sys.exit(f\"error: ch$ch is {g['core:datatype']} at {g['core:sample_rate']:g} S/s, \"
             f\"but gps_l1_l5_file.conf expects ci8 at {$rate:g} S/s\")
print(f\"ch$ch: {g['core:datatype']}, {g['core:sample_rate'] / 1e6:g} MS/s, \"
      f\"{g['x310:lost_samples']} lost samples\")"
done

# 2. extract only the data files (the metadata was just read straight from the archive) to /tmp
need_kb=$(( $(stat -c %s "$archive") / 1024 ))
free_kb=$(df --output=avail -k /tmp | tail -1)
(( free_kb > need_kb )) || { echo "error: /tmp has $((free_kb / 1024 / 1024)) GB free, need $((need_kb / 1024 / 1024)) GB" >&2; exit 1; }
extract_dir="$(mktemp -d "/tmp/gnss_sdr_${name}.XXXXXX")"
# 4. delete the extracted files however the script ends (normal exit, error, Ctrl+C, kill)
trap 'rm -rf "$extract_dir"; echo "removed $extract_dir"' EXIT
trap 'exit 130' INT TERM
echo "extracting to $extract_dir ..."
tar -xf "$archive" -C "$extract_dir" --strip-components=1 "$name/${name}_ch0.sigmf-data" "$name/${name}_ch1.sigmf-data"
data0="$extract_dir/${name}_ch0.sigmf-data"
data1="$extract_dir/${name}_ch1.sigmf-data"

# 3. run gnss-sdr with this recording's files swapped into a copy of the config
mkdir -p "$out_dir"
sed -e "s|^SignalSource0.filename=.*|SignalSource0.filename=$data0|" \
    -e "s|^SignalSource1.filename=.*|SignalSource1.filename=$data1|" \
    "$CONF" > "$out_dir/gps_l1_l5_file.conf"
echo "running gnss-sdr, outputs in $out_dir ..."
cd "$out_dir"
gnss-sdr --config_file=gps_l1_l5_file.conf --log_dir="$out_dir"
