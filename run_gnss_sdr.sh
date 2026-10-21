#!/usr/bin/env bash
# Run GNSS-SDR with a given config (--conf; default gps_l1_l5_file.conf) on one two-channel x310usrp_record.py
# recording (ch0 = L1, ch1 = the second band).
#
# Usage:
#   ./run_gnss_sdr.sh [--conf <file>] [--dump-observables] [--l1-pvt] [recordings/<name>.sigmf]    # default: newest in recordings/
#   ./run_gnss_sdr.sh [...] staging/<name>                 # a recording left as loose files, not bundled
#
#   The loose form (<dir>/<name>_ch{0,1}.sigmf-{meta,data}, e.g. when bundling into the .sigmf archive failed
#   for lack of disk space) is read in place: nothing is extracted, so /tmp needs no free space.
#
#   --conf <file>       gnss-sdr config to run, e.g. gps_l1_l5_file.conf (the default: L1 at 4 MS/s + L5 at 20 MS/s)
#                       or l1ca4msps_l2c4msps.conf (L1 + L2C, both at 4 MS/s). It must read the recording with either
#                       two File_Signal_Sources (SignalSource0/1.filename) or one Multichannel_File_Signal_Source
#                       (SignalSource.filename0/1); this script fills in the file names and sample formats
#   --dump-observables  also save every channel's observables (pseudorange, carrier phase, Doppler, ...) per
#                       epoch to gnss_sdr_output/<name>/observables.mat, for compare_pseudoranges.py
#   --l1-pvt            still track both bands, but compute the position from L1 only (PVT.iono_model=Broadcast)
#                       instead of the dual-frequency Iono-Free-LC: a failing dual-frequency solver keeps resetting
#                       the observables, which also hides the second band's measurements from --dump-observables
#
# Steps:
#   1. reads each channel's sample format from the archive's metadata: ci8 (8-bit, x310usrp_record.py with
#      --int8-div) or ci16_le (the X310's sc16 as received, no --int8-div), and checks each channel's rate matches
#      the config's sampling_frequency for it (e.g. gps_l1_l5_file.conf: ch0 at 4 MS/s, ch1 at 20 MS/s)
#   2. extracts the two .sigmf-data files to a temporary directory in /tmp
#   3. runs gnss-sdr with a copy of the config pointing at those files, with the source's item_type and each
#      data type adapter set to that channel's format (ibyte/Ibyte_To_Complex or ishort/Ishort_To_Complex), inside
#      gnss_sdr_output/<name>/, so its outputs (PVT/RINEX/KML/...) and logs stay per recording
#   4. deletes the extracted files on exit -- also on an error or Ctrl+C

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="$REPO_DIR/gps_l1_l5_file.conf"

dump_observables=false
l1_pvt=false
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --conf) CONF="$(realpath "${2:?--conf needs a file}")"; shift ;;
        --dump-observables) dump_observables=true ;;
        --l1-pvt) l1_pvt=true ;;
        *) echo "error: unknown option $1" >&2; exit 1 ;;
    esac
    shift
done
[[ -f "$CONF" ]] || { echo "error: config $CONF not found" >&2; exit 1; }
# the config's value of a key (empty if absent)
conf_get() { sed -n "s|^$1=||p" "$CONF" | head -1; }
# one Multichannel_File_Signal_Source (SignalSource.*, both files at one rate and format) or two File_Signal_Sources
# (SignalSource0.*, SignalSource1.*)
multichannel=false
[[ "$(conf_get SignalSource.implementation)" == Multichannel_File_Signal_Source ]] && multichannel=true
if $multichannel; then
    rates=("$(conf_get SignalSource.sampling_frequency)" "$(conf_get SignalSource.sampling_frequency)")
else
    rates=("$(conf_get SignalSource0.sampling_frequency)" "$(conf_get SignalSource1.sampling_frequency)")
fi

archive="${1:-$(ls -t "$REPO_DIR"/recordings/*.sigmf 2>/dev/null | head -1)}"
loose=false
if [[ -n "$archive" && ! -f "$archive" && -f "${archive}_ch0.sigmf-meta" && -f "${archive}_ch1.sigmf-meta" ]]; then
    loose=true  # <dir>/<name> given: the per-channel files exist next to each other, unbundled
fi
$loose || [[ -n "$archive" && -f "$archive" ]] || {
    echo "error: give a .sigmf archive or <dir>/<name> of loose <name>_ch*.sigmf-{meta,data} files" >&2; exit 1; }
archive="$(realpath "$archive")"
name="$(basename "$archive" .sigmf)"
out_dir="$REPO_DIR/gnss_sdr_output/$name"
echo "recording: $archive$($loose && echo " (loose files)")"

# the .sigmf-meta of channel $1, from the archive or straight from disk
read_meta() {
    if $loose; then cat "${archive}_ch$1.sigmf-meta"; else tar -xOf "$archive" "$name/${name}_ch$1.sigmf-meta"; fi
}

# 1. read each channel's sample format; the config fixes the rates, so refuse recordings that don't match.
# SigMF datatype -> gnss-sdr File_Signal_Source item_type and the data type adapter that turns it into complex
declare -A ITEM_TYPE=([ci8]=ibyte [ci16_le]=ishort)
declare -A ADAPTER=([ci8]=Ibyte_To_Complex [ci16_le]=Ishort_To_Complex)
datatype=()
for ch in 0 1; do
    rate=${rates[ch]}
    # prints this channel's datatype on stdout (captured) and a summary line on stderr (shown)
    datatype[ch]=$(read_meta "$ch" | python3 -c "
import json, sys
g = json.load(sys.stdin)['global']
if g['core:datatype'] not in ('ci8', 'ci16_le') or g['core:sample_rate'] != $rate:
    sys.exit(f\"error: ch$ch is {g['core:datatype']} at {g['core:sample_rate']:g} S/s, \"
             f\"but $(basename "$CONF") expects ci8 or ci16_le at {$rate:g} S/s\")
print(f\"ch$ch: {g['core:datatype']}, {g['core:sample_rate'] / 1e6:g} MS/s, \"
      f\"{g['x310:lost_samples']} lost samples\", file=sys.stderr)
print(g['core:datatype'])")
done
if $multichannel && [[ "${datatype[0]}" != "${datatype[1]}" ]]; then
    echo "error: ch0 is ${datatype[0]} and ch1 is ${datatype[1]}, but $(basename "$CONF") reads both files with one" \
         "item_type (Multichannel_File_Signal_Source)" >&2
    exit 1
fi

# 2. extract only the data files (the metadata was just read straight from the archive) to /tmp;
# loose files are used where they are
if $loose; then
    data0="${archive}_ch0.sigmf-data"
    data1="${archive}_ch1.sigmf-data"
else
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
fi

# 3. run gnss-sdr with this recording's files swapped into a copy of the config
mkdir -p "$out_dir"
run_conf="$out_dir/$(basename "$CONF")"
# a config of a previous run with another --conf would confuse compare_pseudoranges.py, which reads the only *.conf
find "$out_dir" -maxdepth 1 -name '*.conf' -delete
sed -e "s|^SignalSource0.filename=.*|SignalSource0.filename=$data0|" \
    -e "s|^SignalSource1.filename=.*|SignalSource1.filename=$data1|" \
    -e "s|^SignalSource.filename0=.*|SignalSource.filename0=$data0|" \
    -e "s|^SignalSource.filename1=.*|SignalSource.filename1=$data1|" \
    -e "s|^SignalSource0.item_type=.*|SignalSource0.item_type=${ITEM_TYPE[${datatype[0]}]}|" \
    -e "s|^SignalSource1.item_type=.*|SignalSource1.item_type=${ITEM_TYPE[${datatype[1]}]}|" \
    -e "s|^SignalSource.item_type=.*|SignalSource.item_type=${ITEM_TYPE[${datatype[0]}]}|" \
    -e "s|^DataTypeAdapter0.implementation=.*|DataTypeAdapter0.implementation=${ADAPTER[${datatype[0]}]}|" \
    -e "s|^DataTypeAdapter1.implementation=.*|DataTypeAdapter1.implementation=${ADAPTER[${datatype[1]}]}|" \
    "$CONF" > "$run_conf"
if $l1_pvt; then
    sed -i "s|^PVT.iono_model=.*|PVT.iono_model=Broadcast|" "$run_conf"
    echo "PVT from L1 only (iono_model=Broadcast); the second band is still tracked"
fi
if $dump_observables; then
    # gnss-sdr writes observables.dat (binary) and, with dump_mat, observables.mat next to it
    printf '%s\n' "" "; added by run_gnss_sdr.sh --dump-observables" \
        "Observables.dump=true" "Observables.dump_filename=observables.dat" "Observables.dump_mat=true" \
        >> "$run_conf"
    echo "dumping observables to $out_dir/observables.mat"
fi
echo "sample format: ch0 ${datatype[0]} (${ITEM_TYPE[${datatype[0]}]}), ch1 ${datatype[1]} (${ITEM_TYPE[${datatype[1]}]})"
echo "running gnss-sdr, outputs in $out_dir ..."
cd "$out_dir"
gnss-sdr --config_file="$run_conf" --log_dir="$out_dir"
