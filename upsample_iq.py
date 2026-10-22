"""Upsample interleaved I/Q samples by an integer factor (zero-order hold: each I/Q pair repeated k times).

Used by run_gnss_sdr.sh when a gnss-sdr config reads both channels at one rate (Multichannel_File_Signal_Source)
but a channel was recorded slower, e.g. L1 at 4 MS/s next to L5 at 20 MS/s (k = 5). Zero-order hold is what
gnss-sdr's own Direct_Resampler does; for a ~2 MHz wide signal like L1 C/A its images are harmless.

Reads stdin (or a file) and writes the output file in chunks, so memory stays small for any file size.

Example:
  tar -xOf rec.sigmf rec/rec_ch0.sigmf-data | python3 upsample_iq.py --factor 5 --datatype ci8 - /tmp/ch0.dat
"""

import argparse
import sys

import numpy as np

# SigMF datatype -> numpy dtype holding ONE whole I/Q pair, so a pair is repeated as a unit
PAIR_DTYPES = {"ci8": np.uint16, "ci16_le": np.uint32}
CHUNK_PAIRS = 4_000_000  # pairs read per chunk (1 s at 4 MS/s)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="input .sigmf-data, or - for stdin")
    p.add_argument("output", help="output file")
    p.add_argument("--factor", type=int, required=True, help="upsampling factor k (integer >= 1)")
    p.add_argument("--datatype", choices=sorted(PAIR_DTYPES), required=True, help="SigMF datatype of the samples")
    return p.parse_args()


def main():
    args = parse_args()
    if args.factor < 1:
        raise SystemExit("error: --factor must be >= 1")
    dtype = np.dtype(PAIR_DTYPES[args.datatype])
    src = sys.stdin.buffer if args.input == "-" else open(args.input, "rb")
    with src, open(args.output, "wb") as out:
        while True:
            raw = src.read(CHUNK_PAIRS * dtype.itemsize)
            if not raw:
                break
            pairs = np.frombuffer(raw, dtype=dtype, count=len(raw) // dtype.itemsize)
            np.repeat(pairs, args.factor).tofile(out)


if __name__ == "__main__":
    main()
