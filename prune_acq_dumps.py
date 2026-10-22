"""Keep gnss-sdr's acquisition dumps small while it runs.

With Acquisition_<sig>.dump=true, gnss-sdr writes one .mat file per acquisition attempt, each holding the full
delay-Doppler grid (7 MB for L5 at 20 MS/s), and a 5-min run makes thousands of attempts (15 GB seen). This script
runs next to gnss-sdr (started by run_gnss_sdr.sh --report) and, every few seconds:
  1. appends every new file's scalars (band, PRN, detected?, Doppler, delay, test statistic, threshold, ...) to
     acq_summary.csv, so the report still sees every attempt;
  2. deletes the grid file, except the --keep strongest DETECTED ones per band (largest test statistic), which
     gnss_sdr_report.py plots as delay-Doppler maps.
It stops after the process --pid exits, with a last pass over whatever is left.

Example:
  uv run prune_acq_dumps.py gnss_sdr_output/<name> --pid 12345
"""

import argparse
import csv
import os
import re
import time
from pathlib import Path

import h5py

# acq_<sig>_G_<sig>_ch_<ch>_<attempt>_sat_<PRN>.mat, as gnss-sdr names them with dump_filename=acq_<sig>
NAME_RE = re.compile(r"^acq_(?P<sig>[^_]+)_.*_ch_(?P<ch>\d+)_(?P<attempt>\d+)_sat_(?P<prn>\d+)\.mat$")
FIELDS = ["acq_doppler_hz", "acq_delay_samples", "test_statistic", "threshold", "doppler_max", "doppler_step",
          "sample_counter", "input_power"]
COLUMNS = ["file", "band", "channel", "attempt", "prn", "detected", *FIELDS]
SETTLE_SEC = 2.0  # skip files modified more recently than this: gnss-sdr may still be writing them
POLL_SEC = 3.0


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run_dir", type=Path, help="gnss-sdr output directory holding the acq_*.mat dumps")
    p.add_argument("--pid", type=int, default=None, help="keep pruning until this process exits (default: one pass)")
    p.add_argument("--keep", type=int, default=2, help="detected grids kept per band (default: 2)")
    return p.parse_args()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_row(path, m):
    with h5py.File(path, "r") as f:
        values = {k: float(f[k][()].ravel()[0]) for k in FIELDS if k in f}
        detected = int(float(f["d_positive_acq"][()].ravel()[0]) > 0)
    return {"file": path.name, "band": m["sig"], "channel": int(m["ch"]), "attempt": int(m["attempt"]),
            "prn": int(m["prn"]), "detected": detected, **values}


def prune_pass(run_dir, keep, kept, writer, final):
    """kept: {band: [(test_statistic, path)]} of the detected grids currently kept."""
    now = time.time()
    for path in sorted(run_dir.glob("acq_*.mat")):
        m = NAME_RE.match(path.name)
        if not m or any(path == p for grids in kept.values() for _, p in grids):
            continue
        if not final and now - path.stat().st_mtime < SETTLE_SEC:
            continue
        try:
            row = read_row(path, m)
        except (OSError, KeyError):
            if final:
                path.unlink(missing_ok=True)  # truncated by a killed run
            continue
        writer.writerow(row)
        grids = kept.setdefault(row["band"], [])
        if row["detected"]:
            grids.append((row["test_statistic"], path))
            grids.sort(key=lambda g: g[0], reverse=True)
            for _, dropped in grids[keep:]:
                dropped.unlink(missing_ok=True)
            del grids[keep:]
        else:
            path.unlink(missing_ok=True)


def main():
    args = parse_args()
    summary = args.run_dir / "acq_summary.csv"
    new_file = not summary.exists()
    kept = {}
    with open(summary, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if new_file:
            writer.writeheader()
        while args.pid is not None and alive(args.pid):
            prune_pass(args.run_dir, args.keep, kept, writer, final=False)
            f.flush()
            time.sleep(POLL_SEC)
        prune_pass(args.run_dir, args.keep, kept, writer, final=True)


if __name__ == "__main__":
    main()
