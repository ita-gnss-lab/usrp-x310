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
#   --report            also dump what gnss_sdr_out2tex.py needs to show every receiver stage: the acquisition grids of
#                       the first channel of each band, every channel's tracking and navigation-decoder outputs, the
#                       observables and the PVT solutions (implies --dump-observables)
#   --l1-pvt            still track both bands, but compute the position from L1 only (PVT.iono_model=Broadcast)
#                       instead of the dual-frequency Iono-Free-LC: a failing dual-frequency solver keeps resetting
#                       the observables, which also hides the second band's measurements from --dump-observables
#
# Steps:
#   1. reads each channel's sample format from the archive's metadata: ci8 (8-bit, x310usrp_record.py with
#      --int8-div) or ci16_le (the X310's sc16 as received, no --int8-div), and checks each channel's rate matches
#      the config's sampling_frequency for it (e.g. gps_l1_l5_file.conf: ch0 at 4 MS/s, ch1 at 20 MS/s). With a
#      Multichannel_File_Signal_Source, a channel recorded k times slower than the config's rate (an integer k) is
#      accepted too and upsampled k times (upsample_iq.py, zero-order hold), e.g. L1 4 -> 20 MS/s next to L5
#   2. extracts the two .sigmf-data files (upsampled where needed) to a temporary directory in /tmp
#   3. runs gnss-sdr with a copy of the config pointing at those files, with the source's item_type and each
#      data type adapter set to that channel's format (ibyte/Ibyte_To_Complex or ishort/Ishort_To_Complex), inside
#      gnss_sdr_output/<name>/, so its outputs (PVT/RINEX/KML/...) and logs stay per recording
#   4. deletes the extracted files on exit -- also on an error or Ctrl+C

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
UV="$(command -v uv || echo /home/tapyu/.local/bin/uv)"  # systemd services don't have ~/.local/bin in PATH
CONF="$REPO_DIR/gps_l1_l5_file.conf"

dump_observables=false
report=false
l1_pvt=false
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --conf) CONF="$(realpath "${2:?--conf needs a file}")"; shift ;;
        --dump-observables) dump_observables=true ;;
        --report) report=true; dump_observables=true ;;
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

# 1. read each channel's sample format; the config fixes the rates, so refuse recordings that don't match (or, with a
# Multichannel_File_Signal_Source, that are not an integer factor slower: those get upsampled).
# SigMF datatype -> gnss-sdr File_Signal_Source item_type and the data type adapter that turns it into complex
declare -A ITEM_TYPE=([ci8]=ibyte [ci16_le]=ishort)
declare -A ADAPTER=([ci8]=Ibyte_To_Complex [ci16_le]=Ishort_To_Complex)
datatype=()
factor=()  # upsampling factor per channel (1: used as recorded)
for ch in 0 1; do
    rate=${rates[ch]}
    # prints this channel's datatype and upsampling factor on stdout (captured) and a summary line on stderr (shown)
    read -r datatype[ch] factor[ch] < <(read_meta "$ch" | python3 -c "
import json, sys
g = json.load(sys.stdin)['global']
fs = g['core:sample_rate']
k = round($rate / fs) if fs else 0
if g['core:datatype'] not in ('ci8', 'ci16_le') or not (fs == $rate or ($($multichannel && echo True || echo False) and k > 1 and k * fs == $rate)):
    sys.exit(f\"error: ch$ch is {g['core:datatype']} at {fs:g} S/s, \"
             f\"but $(basename "$CONF") expects ci8 or ci16_le at {$rate:g} S/s\")
print(f\"ch$ch: {g['core:datatype']}, {fs / 1e6:g} MS/s{f', upsampled x{k}' if k > 1 else ''}, \"
      f\"{g['x310:lost_samples']} lost samples\", file=sys.stderr)
print(g['core:datatype'], k)")
    [[ -n "${datatype[ch]}" ]] || exit 1  # the check above failed (its error is already printed)
done
if $multichannel && [[ "${datatype[0]}" != "${datatype[1]}" ]]; then
    echo "error: ch0 is ${datatype[0]} and ch1 is ${datatype[1]}, but $(basename "$CONF") reads both files with one" \
         "item_type (Multichannel_File_Signal_Source)" >&2
    exit 1
fi

# 2. extract only the data files (the metadata was just read straight from the archive) to /tmp, upsampling the
# channels that need it; loose files that need no upsampling are used where they are
member() { echo "$name/${name}_ch$1.sigmf-data"; }
# the size of channel $1's data file, in kB
data_kb() {
    if $loose; then stat -c %s "${archive}_ch$1.sigmf-data"
    else tar -tvf "$archive" "$(member "$1")" | awk '{print $3}'; fi | awk '{printf "%d", $1 / 1024}'
}
need_kb=0
for ch in 0 1; do
    if ! $loose || (( factor[ch] > 1 )); then need_kb=$(( need_kb + $(data_kb "$ch") * factor[ch] )); fi
done
data=()
if (( need_kb > 0 )); then
    free_kb=$(df --output=avail -k /tmp | tail -1)
    (( free_kb > need_kb )) || { echo "error: /tmp has $((free_kb / 1024 / 1024)) GB free, need $((need_kb / 1024 / 1024)) GB" >&2; exit 1; }
    extract_dir="$(mktemp -d "/tmp/gnss_sdr_${name}.XXXXXX")"
    # 4. delete the extracted files however the script ends (normal exit, error, Ctrl+C, kill)
    trap 'rm -rf "$extract_dir"; echo "removed $extract_dir"' EXIT
    trap '[[ -n ${gnss_pid:-} ]] && kill "$gnss_pid" 2>/dev/null; exit 130' INT TERM
fi
for ch in 0 1; do
    if $loose && (( factor[ch] == 1 )); then
        data[ch]="${archive}_ch$ch.sigmf-data"
        continue
    fi
    data[ch]="$extract_dir/${name}_ch$ch.sigmf-data"
    if (( factor[ch] > 1 )); then
        echo "extracting ch$ch to $extract_dir, upsampled x${factor[ch]} ..."
        if $loose; then cat "${archive}_ch$ch.sigmf-data"; else tar -xOf "$archive" "$(member "$ch")"; fi |
            python3 "$REPO_DIR/upsample_iq.py" --factor "${factor[ch]}" --datatype "${datatype[ch]}" - "${data[ch]}"
    else
        echo "extracting ch$ch to $extract_dir ..."
        tar -xf "$archive" -C "$extract_dir" --strip-components=1 "$(member "$ch")"
    fi
done
data0=${data[0]}
data1=${data[1]}

# 3. run gnss-sdr with this recording's files swapped into a copy of the config. Outputs of a previous run of this
# recording are removed first: stale RINEX/dump files would be mixed with the new ones (and compare_pseudoranges.py
# reads the only *.conf)
rm -rf "$out_dir"
mkdir -p "$out_dir"
run_conf="$out_dir/$(basename "$CONF")"
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
if $report; then
    # per band (gnss-sdr signal code, in its channel order 1C, 2S, L5): acquisition grids of the band's first channel
    # only (every search attempt is dumped, so all channels would be thousands of files), every channel's tracking
    # (trk_<sig>_<ch>.mat) and navigation decoder (tlm_<sig>_<ch>.mat); then the PVT solutions (pvt.mat)
    # A band whose search grid exceeds 64 MB per attempt gets no acquisition dump: writing it slows gnss-sdr to a crawl
    # (L2C: one 20-ms CM code period x 1000 Doppler bins of 10 Hz = 320 MB per attempt at 4 MS/s)
    lines=("" "; added by run_gnss_sdr.sh --report")
    declare -A CODE_MS=([1C]=1 [2S]=20 [L5]=1)  # samples searched per attempt: one code period (coherent_integration_time_ms=1)
    first_ch=0
    fs_internal=$(conf_get GNSS-SDR.internal_fs_sps)
    for sig in 1C 2S L5; do
        n=$(conf_get "Channels_$sig.count"); n=${n:-0}
        (( n > 0 )) || continue
        dmax=$(conf_get "Acquisition_$sig.doppler_max"); dstep=$(conf_get "Acquisition_$sig.doppler_step")
        grid_mb=$(( ${fs_internal%.*} * CODE_MS[$sig] / 1000 * 2 * ${dmax:-5000} / ${dstep:-250} * 4 / 1000000 ))
        if (( grid_mb <= 64 )); then
            lines+=("Acquisition_$sig.dump=true" "Acquisition_$sig.dump_filename=acq_$sig" "Acquisition_$sig.dump_channel=$first_ch")
        else
            echo "no acquisition dump for $sig: ${grid_mb} MB per search attempt"
        fi
        lines+=("Tracking_$sig.dump=true" "Tracking_$sig.dump_filename=trk_${sig}_" "Tracking_$sig.dump_mat=true"
                "TelemetryDecoder_$sig.dump=true" "TelemetryDecoder_$sig.dump_filename=tlm_${sig}_")
        first_ch=$(( first_ch + n ))
    done
    lines+=("PVT.dump=true" "PVT.dump_filename=pvt.dat" "PVT.dump_mat=true")
    printf '%s\n' "${lines[@]}" >> "$run_conf"
    echo "dumping every stage's outputs for gnss_sdr_out2tex.py to $out_dir"
fi
echo "sample format: ch0 ${datatype[0]} (${ITEM_TYPE[${datatype[0]}]}), ch1 ${datatype[1]} (${ITEM_TYPE[${datatype[1]}]})"
echo "running gnss-sdr, outputs in $out_dir ..."
cd "$out_dir"
if $report; then
    # gnss-sdr dumps one acquisition grid file per search attempt (thousands per run, up to 7 MB each):
    # prune_acq_dumps.py logs them all to acq_summary.csv and keeps only the strongest detections' grids
    # its console output (decoded navigation messages, positions) is kept in gnss-sdr.stdout for the report too
    gnss-sdr --config_file="$run_conf" --log_dir="$out_dir" > >(tee "$out_dir/gnss-sdr.stdout") 2>&1 &
    gnss_pid=$!
    "$UV" run --project "$REPO_DIR" "$REPO_DIR/prune_acq_dumps.py" "$out_dir" --pid "$gnss_pid" &
    prune_pid=$!
    wait "$gnss_pid"
    wait "$prune_pid"
else
    gnss-sdr --config_file="$run_conf" --log_dir="$out_dir"
fi
