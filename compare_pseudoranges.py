"""Compare each satellite's L5 and L1 pseudoranges from a gnss-sdr observables dump.

For a satellite tracked on both bands, at the same epoch:

    P_L5 - P_L1 = extra ionospheric delay on L5 (~0.8x the L1 delay, i.e. a few to a few tens of metres)
                  + a constant bias (receiver hardware/processing, the same for every satellite)

so a healthy dual-frequency setup shows differences of metres to tens of metres, nearly equal across
satellites and drifting slowly. Hundreds of metres or more, very different values per satellite, or jumps
point at a timing/ambiguity problem in one of the two paths -- which is what the dual-frequency
(Iono-Free-LC) solver would reject.

The dump comes from:
  ./gps_l1_l5_file.sh --dump-observables staging/<name>.sigmf
which writes gnss_sdr_output/<name>/observables.mat. Channels are mapped to bands from that run's config
(the first Channels_1C.count channels are L1 C/A, the next Channels_L5.count are L5).

Example:
  uv run compare_pseudoranges.py gnss_sdr_output/x310_20261020T182428Z
"""

import argparse
import re
from pathlib import Path

import numpy as np

FIELDS = ["RX_time", "Pseudorange_m", "PRN", "Flag_valid_pseudorange"]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run_dir", type=Path, help="gnss_sdr_output/<name> folder of a --dump-observables run")
    return p.parse_args()


def channel_counts(conf_path):
    """(number of L1 C/A channels, number of L5 channels) from the run's gnss-sdr config."""
    text = conf_path.read_text()
    n_1c = int(re.search(r"^Channels_1C\.count=(\d+)", text, re.M).group(1))
    n_l5 = int(re.search(r"^Channels_L5\.count=(\d+)", text, re.M).group(1))
    return n_1c, n_l5


def load_observables(mat_path, n_channels):
    """Each field as a (n_channels, n_epochs) array. gnss-sdr writes MAT v7.3 (HDF5), read with h5py;
    an older v5 file is read with scipy instead."""
    import h5py
    if h5py.is_hdf5(mat_path):  # v7.3
        with h5py.File(mat_path, "r") as f:
            data = {k: np.asarray(f[k], dtype=float) for k in FIELDS}
    else:
        from scipy.io import loadmat
        raw = loadmat(mat_path)
        data = {k: np.asarray(raw[k], dtype=float) for k in FIELDS}
    for k, v in data.items():
        if v.shape[0] != n_channels and v.shape[-1] == n_channels:  # HDF5 stores MATLAB arrays transposed
            data[k] = v.T
    return data


def main():
    args = parse_args()
    n_1c, n_l5 = channel_counts(args.run_dir / "gps_l1_l5_file.conf")
    obs = load_observables(args.run_dir / "observables.mat", n_1c + n_l5)
    rx_time = obs["RX_time"].max(axis=0)  # receiver time of each epoch (same for every channel)
    valid = obs["Flag_valid_pseudorange"] > 0
    print(f"{obs['Pseudorange_m'].shape[1]:,} epochs, channels 0-{n_1c - 1} = L1 C/A, "
          f"{n_1c}-{n_1c + n_l5 - 1} = L5\n")

    # per epoch and PRN, the pseudorange on each band (a PRN can move between channels after a loss of lock)
    def by_prn(channels):
        out = {}
        for ch in channels:
            for e in np.flatnonzero(valid[ch]):
                out.setdefault(int(obs["PRN"][ch, e]), {})[e] = obs["Pseudorange_m"][ch, e]
        return out
    p1, p5 = by_prn(range(n_1c)), by_prn(range(n_1c, n_1c + n_l5))

    diffs = {}  # PRN -> (epochs, P_L5 - P_L1)
    print(f"{'PRN':>4} {'epochs':>7} {'span [s]':>9} {'mean [m]':>12} {'std [m]':>9} {'min [m]':>12} "
          f"{'max [m]':>12} {'drift [m/s]':>12}")
    for prn in sorted(set(p1) & set(p5)):
        epochs = np.array(sorted(set(p1[prn]) & set(p5[prn])))
        if len(epochs) < 2:
            continue
        d = np.array([p5[prn][e] - p1[prn][e] for e in epochs])
        t = rx_time[epochs]
        drift = np.polyfit(t - t[0], d, 1)[0]
        diffs[prn] = (epochs, d)
        print(f"{prn:>4} {len(epochs):>7} {t[-1] - t[0]:>9.1f} {d.mean():>12.2f} {d.std():>9.2f} "
              f"{d.min():>12.2f} {d.max():>12.2f} {drift:>12.4f}")
    if not diffs:
        raise SystemExit("no satellite has valid L1 and L5 pseudoranges at the same epochs")

    # the constant bias is common to all satellites: at each epoch, how far is each PRN from the others?
    per_epoch = {}  # epoch -> P_L5 - P_L1 of every PRN valid then
    for ep, d in diffs.values():
        for e, v in zip(ep, d):
            per_epoch.setdefault(e, []).append(v)
    spread = [max(vals) - min(vals) for vals in per_epoch.values() if len(vals) >= 2]
    if spread:
        print(f"\nspread of P_L5 - P_L1 across satellites at the same epoch: median {np.median(spread):.2f} m, "
              f"max {np.max(spread):.2f} m")
    print("expected if healthy: |mean| of metres to tens of metres, similar for every PRN (a shared bias),\n"
          "std of a few metres, small drift; spread across satellites of a few to ~20 m (ionosphere only)")


if __name__ == "__main__":
    main()
