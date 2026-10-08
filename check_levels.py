"""Check per-channel I/Q sample statistics from a SigMF Archive, to help pick --gain.

GNSS signals sit 15-20 dB below the thermal noise floor, so there's no "signal" to
look for here -- gain-setting is about placing the *noise floor* correctly within the
ADC's dynamic range: high enough that quantization noise doesn't matter, low enough
that strong interferers don't clip the ADC rails.

Rule of thumb: aim for each channel's I/Q standard deviation around 1/4 to 1/3 ADC full-scale
input level (which stored as a int16, with 32768), with near-zero samples at the rails (clipping).
  - std-dev too low  (e.g. < 2000, which is 1/16 of full-scale)  -> raise --gain, you're wasting ADC bits
  - std-dev too high (e.g. > 12000, which is 1/3 of full-scale)  -> lower --gain, you're close to clipping
  - clipping % > ~0                                              -> lower --gain regardless of std-dev

Example:
  uv run check_levels.py recordings/x310_20261009T160000Z.sigmf
"""

import argparse
import tarfile
from pathlib import Path

import numpy as np

FULL_SCALE = 32768  # int16 magnitude ceiling
TARGET_LOW, TARGET_HIGH = FULL_SCALE // 16, FULL_SCALE // 3  # ~2048-10922


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("archive", type=Path, help="a .sigmf archive from x310usrp_record.py")
    p.add_argument("--max-samples", type=int, default=2_000_000,
                   help="cap how many I/Q samples to read per channel, for speed (default: 2e6)")
    return p.parse_args()


def report_channel(name, iq):
    i, q = iq[0::2].astype(np.int32), iq[1::2].astype(np.int32)
    i_std, q_std = i.std(), q.std()
    # gives the fraction of samples that are at or near the ADC rails/limits (clipping)
    clipped_pct = np.mean((np.abs(i) >= FULL_SCALE - 1) | (np.abs(q) >= FULL_SCALE - 1)) * 100

    verdict = "OK"
    if clipped_pct > 0.01:
        verdict = "CLIPPING -- lower --gain"
    elif max(i_std, q_std) < TARGET_LOW:
        verdict = "too low -- raise --gain"
    elif max(i_std, q_std) > TARGET_HIGH:
        verdict = "too high -- lower --gain"

    print(f"{name}: I std={i_std:.0f}  Q std={q_std:.0f}  "
          f"(target {TARGET_LOW}-{TARGET_HIGH} of {FULL_SCALE})  "
          f"clipping={clipped_pct:.3f}%  -> {verdict}")


def main():
    args = parse_args()
    with tarfile.open(args.archive) as tar:
        data_members = sorted((m for m in tar.getmembers() if m.name.endswith(".sigmf-data")),
                               key=lambda m: m.name)
        if not data_members:
            raise SystemExit(f"error: no .sigmf-data members found in {args.archive}")
        for member in data_members:
            n_bytes = min(member.size, args.max_samples * 4)  # sc16 = 4 B/sample
            raw = tar.extractfile(member).read(n_bytes)
            iq = np.frombuffer(raw, dtype=np.int16)
            report_channel(Path(member.name).name, iq)


if __name__ == "__main__":
    main()
