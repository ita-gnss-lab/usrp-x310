"""Record I/Q samples from the USRP X310 + TwinRX to SigMF files.

One recording per channel: <name>_ch<k>.sigmf-data (interleaved int16 I/Q,
little-endian) + <name>_ch<k>.sigmf-meta (JSON). Defaults reproduce the MATLAB
setup in coleta_dados_v3.m: GPS L1 on TwinRX RX0, GPS L5 on TwinRX RX1.
"""

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import uhd

SUBDEV = "A:0 A:1"  # TwinRX RX0 and RX1 on daughterboard slot A
START_DELAY = 0.5  # s, lets both channels start on the same sample
LOCK_TIMEOUT = 5.0  # s
LINK_LIMIT = 100e6  # B/s, practical payload limit of 1 GbE


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--args", default="addr=192.168.10.2", help="UHD device args")
    p.add_argument("--freq", type=float, nargs="+", default=[1575.42e6, 1176.45e6],
                   help="center frequency per channel [Hz]")
    p.add_argument("--rate", type=float, default=2e6, help="sample rate [S/s]")
    p.add_argument("--gain", type=float, nargs="+", default=[1.0], help="gain [dB], one value or one per channel")
    p.add_argument("--duration", type=float, default=10.0, help="recording length [s]")
    p.add_argument("--channels", type=int, nargs="+", default=[0, 1], help="channel indices (0=RX0, 1=RX1)")
    p.add_argument("--clock-source", default="external", help="10 MHz reference: external | internal")
    p.add_argument("--time-source", default="internal", help="PPS source: internal | external")
    p.add_argument("--out-dir", type=Path, default=Path("recordings"))
    p.add_argument("--name", default=None, help="file prefix (default: UTC timestamp)")
    args = p.parse_args()

    n = len(args.channels)
    if len(args.freq) == 1:
        args.freq *= n
    if len(args.gain) == 1:
        args.gain *= n
    if len(args.freq) != n or len(args.gain) != n:
        p.error("--freq and --gain need either one value or one value per channel")
    return args


def configure(usrp, args):
    """Apply clock/time sources, frontends, rate, frequency and gain; return the actual settings."""
    usrp.set_clock_source(args.clock_source)
    usrp.set_time_source(args.time_source)
    usrp.set_rx_subdev_spec(uhd.usrp.SubdevSpec(SUBDEV))

    settings = []
    for ch, freq, gain in zip(args.channels, args.freq, args.gain):
        usrp.set_rx_rate(args.rate, ch)
        usrp.set_rx_freq(uhd.types.TuneRequest(freq), ch)
        usrp.set_rx_gain(gain, ch)
        settings.append({
            "channel": ch,
            "rate": usrp.get_rx_rate(ch),
            "freq": usrp.get_rx_freq(ch),
            "gain": usrp.get_rx_gain(ch),
            "antenna": usrp.get_rx_antenna(ch),
            "frontend": usrp.get_rx_subdev_name(ch),
        })

    for s in settings:
        print(f"ch{s['channel']} ({s['frontend']}, {s['antenna']}): "
              f"freq={s['freq'] / 1e6:.6f} MHz  rate={s['rate'] / 1e6:.6f} MS/s  gain={s['gain']:.1f} dB")
    if abs(settings[0]["rate"] - args.rate) > 1e-3:
        print(f"note: requested rate {args.rate / 1e6} MS/s coerced to {settings[0]['rate'] / 1e6} MS/s")

    throughput = len(settings) * settings[0]["rate"] * 4  # sc16 = 4 B/sample
    if throughput > LINK_LIMIT:
        print(f"warning: {throughput / 1e6:.0f} MB/s exceeds what 1 GbE can carry; expect overflows")
    return settings


def wait_for_sensor(read, name):
    deadline = time.monotonic() + LOCK_TIMEOUT
    while not read().to_bool():
        if time.monotonic() > deadline:
            raise SystemExit(f"error: {name} not locked after {LOCK_TIMEOUT:.0f} s")
        time.sleep(0.1)
    print(f"{name}: locked")


def wait_for_locks(usrp, args):
    if args.clock_source == "external":
        wait_for_sensor(lambda: usrp.get_mboard_sensor("ref_locked", 0), "ref_locked")
    for ch in args.channels:
        if "lo_locked" in usrp.get_rx_sensor_names(ch):
            wait_for_sensor(lambda: usrp.get_rx_sensor("lo_locked", ch), f"ch{ch} lo_locked")
        else:
            print(f"warning: ch{ch} has no lo_locked sensor, skipping LO lock check")


def record(usrp, args, rate, paths):
    """Stream all channels to their .sigmf-data files. Returns (samples written, gap annotations, start time)."""
    stream_args = uhd.usrp.StreamArgs("sc16", "sc16")
    stream_args.channels = args.channels
    streamer = usrp.get_rx_stream(stream_args)

    # One uint32 per sample (I and Q int16 packed), so recv() sees the right sample count
    spp = streamer.get_max_num_samps()
    buf = np.empty((len(args.channels), spp), dtype=np.uint32)
    md = uhd.types.RXMetadata()

    target = round(args.duration * rate)
    written = 0
    gaps = []

    usrp.set_time_now(uhd.types.TimeSpec(0.0))
    cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
    cmd.stream_now = False
    cmd.time_spec = uhd.types.TimeSpec(START_DELAY)
    start_utc = datetime.now(timezone.utc) + timedelta(seconds=START_DELAY)
    streamer.issue_stream_cmd(cmd)

    files = [open(p, "wb") for p in paths]
    timeout = START_DELAY + 1.0
    try:
        while written < target:
            n = streamer.recv(buf, md, timeout)
            timeout = 0.5
            err = md.error_code
            if err == uhd.types.RXMetadataErrorCode.overflow:
                continue  # the gap is measured from the next packet's timestamp
            if err != uhd.types.RXMetadataErrorCode.none:
                raise RuntimeError(f"receive error: {md.strerror()}")
            if n == 0:
                continue

            if md.has_time_spec:
                expected = round((md.time_spec.get_real_secs() - START_DELAY) * rate)
                if expected > written:
                    gaps.append((written, expected - written))
                    print(f"overflow: {expected - written} samples lost at sample {written}")

            n = min(n, target - written)
            for f, row in zip(files, buf):
                row[:n].tofile(f)
            written += n
    except KeyboardInterrupt:
        print("interrupted, finalizing files")
    finally:
        streamer.issue_stream_cmd(uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont))
        while streamer.recv(buf, md, 0.1):  # drain what is still in flight
            pass
        for f in files:
            f.close()
    return written, gaps, start_utc


def write_meta(path, s, args, start_utc, gaps):
    meta = {
        "global": {
            "core:datatype": "ci16_le",
            "core:sample_rate": s["rate"],
            "core:version": "1.0.0",
            "core:num_channels": 1,
            "core:hw": f"USRP X310 / {s['frontend']}",
            "core:recorder": "UHD Python API (usrp-x310/main.py)",
            "core:description": f"{args.args}, channel {s['channel']}",
            "core:extensions": [{"name": "x310", "version": "0.1.0", "optional": True}],
            "x310:gain_db": s["gain"],
            "x310:antenna": s["antenna"],
            "x310:clock_source": args.clock_source,
            "x310:time_source": args.time_source,
            "x310:dropped_samples": sum(n for _, n in gaps),
        },
        "captures": [{
            "core:sample_start": 0,
            "core:frequency": s["freq"],
            "core:datetime": start_utc.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        }],
        "annotations": [
            {"core:sample_start": start, "core:sample_count": 0,
             "core:comment": "overflow", "x310:dropped_samples": lost}
            for start, lost in gaps
        ],
    }
    path.write_text(json.dumps(meta, indent=2))


def main():
    args = parse_args()
    name = args.name or datetime.now(timezone.utc).strftime("x310_%Y%m%dT%H%M%SZ")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    usrp = uhd.usrp.MultiUSRP(args.args)
    settings = configure(usrp, args)
    wait_for_locks(usrp, args)

    bases = [args.out_dir / f"{name}_ch{s['channel']}" for s in settings]
    data_paths = [b.parent / f"{b.name}.sigmf-data" for b in bases]
    rate = settings[0]["rate"]

    print(f"recording {args.duration} s ...")
    written, gaps, start_utc = record(usrp, args, rate, data_paths)

    for s, base, data in zip(settings, bases, data_paths):
        write_meta(base.parent / f"{base.name}.sigmf-meta", s, args, start_utc, gaps)
        print(f"{data}: {written} samples ({data.stat().st_size / 1e6:.1f} MB)")
    print(f"overflows: {len(gaps)} ({sum(n for _, n in gaps)} samples lost)")


if __name__ == "__main__":
    main()
