"""Check per-channel I/Q sample statistics from a SigMF Archive, to help pick --gain.

GNSS signals sit 15-20 dB below the thermal noise floor, so there's no "signal" to
look for here -- gain-setting is about placing the *noise floor* correctly within the
ADC's dynamic range: high enough that quantization noise doesn't matter, low enough
that strong interferers don't clip the ADC rails.

Rule of thumb: aim for each channel's I/Q standard deviation between 1/16 and 1/3 of the stored
format's full-scale (128 for int8 / ci8, 32768 for int16 / ci16_le, read from each channel's
.sigmf-meta), with near-zero samples at the rails (clipping).
  - std-dev too low  (< 1/16 of full-scale, e.g. 8 for int8)  -> raise --gain, you're wasting bits
  - std-dev too high (> 1/3 of full-scale, e.g. 42 for int8)  -> lower --gain, you're close to clipping
  - clipping % > ~0                                              -> lower --gain regardless of std-dev

Example:
  uv run check_levels.py recordings/x310_20261009T160000Z.sigmf
"""

import argparse
import json
import tarfile
from pathlib import Path

import numpy as np

# SigMF datatype -> (numpy dtype of one I or Q value, magnitude ceiling)
DATATYPES = {"ci8": (np.int8, 128), "ci16_le": (np.int16, 32768)}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("archive", type=Path, help="a .sigmf archive from x310usrp_record.py")
    p.add_argument("--max-samples", type=int, default=2_000_000,
                   help="cap how many I/Q samples to read per channel, for speed (default: 2e6)")
    return p.parse_args()


def report_channel(name, iq, full_scale):
    target_low, target_high = full_scale // 16, full_scale // 3
    i, q = iq[0::2].astype(np.int32), iq[1::2].astype(np.int32)
    i_std, q_std = i.std(), q.std()
    # gives the fraction of samples that are at or near the ADC rails/limits (clipping)
    clipped_pct = np.mean((np.abs(i) >= full_scale - 1) | (np.abs(q) >= full_scale - 1)) * 100

    verdict = "OK"
    if clipped_pct > 0.01:
        verdict = "CLIPPING -- lower --gain"
    elif max(i_std, q_std) < target_low:
        verdict = "too low -- raise --gain"
    elif max(i_std, q_std) > target_high:
        verdict = "too high -- lower --gain"

    print(f"{name}: I std={i_std:.0f}  Q std={q_std:.0f}  "
          f"(target {target_low}-{target_high} of {full_scale})  "
          f"clipping={clipped_pct:.3f}%  -> {verdict}")


def main():
    args = parse_args()
    with tarfile.open(args.archive) as tar:
        data_members = sorted((m for m in tar.getmembers() if m.name.endswith(".sigmf-data")),
                               key=lambda m: m.name)
        if not data_members:
            raise SystemExit(f"error: no .sigmf-data members found in {args.archive}")
        for member in data_members:
            # the format lives in the matching .sigmf-meta, so old (ci16_le) archives still work
            meta = json.loads(tar.extractfile(member.name.removesuffix(".sigmf-data") + ".sigmf-meta").read())
            dtype, full_scale = DATATYPES[meta["global"]["core:datatype"]]
            n_bytes = min(member.size, args.max_samples * 2 * np.dtype(dtype).itemsize)  # I + Q per sample
            raw = tar.extractfile(member).read(n_bytes)
            iq = np.frombuffer(raw, dtype=dtype)
            report_channel(Path(member.name).name, iq, full_scale)


if __name__ == "__main__":
    main()
