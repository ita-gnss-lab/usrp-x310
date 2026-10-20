"""Record I/Q samples from the USRP X310 + TwinRX to SigMF files.

One recording per channel: <name>_ch<k>.sigmf-data (interleaved int16 I/Q, little-endian)
+ <name>_ch<k>.sigmf-meta (JSON).
"""

import argparse
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import uhd
from loguru import logger
from sigmf import SigMFCollection, SigMFFile

SUBDEV = "A:0 A:1"  # TwinRX RX0 and RX1 on daughterboard slot A
# ??? why this delay?
START_DELAY = 0.5  # s, lets both channels start on the same sample
LOCK_TIMEOUT = 5.0  # s
LINK_LIMIT = 100e6  # B/s, practical payload limit of 1 GbE
RECV_CHUNK = 0.01  # s of samples per recv() call, so 20 MS/s isn't ~10k Python iterations/s
GPS_L1_FREQ = 1575.42e6  # Hz
GPS_L5_FREQ = 1176.45e6  # Hz
# default I/Q rate per band: L5's ~20 MHz main lobe (10.23 Mcps) needs 20 MS/s, L1 C/A's ~2 MHz is fine at 4 MS/s
DEFAULT_IQ_RATES = {GPS_L1_FREQ: 4e6, GPS_L5_FREQ: 20e6}  # Hz -> S/s
# OTW (over-the-wire) format: how samples travel from the USRP to the host. The X310 only supports sc16;
# asking UHD for another (e.g. sc8) is accepted silently, but the X310 still sends sc16 and UHD then
# misreads every 16-bit value as an 8-bit I/Q pair (that is what broke the 19-20/10/2026 recordings)
OTW_FORMAT = "sc16"
X310_OTW_FORMATS = {"sc16"}  # the only OTW format the X310 actually sends; checked at startup
# CPU format: what recv() writes into our buffer. sc16 = the OTW samples unchanged (no float conversion);
# UHD 4.6 has no sc16-OTW -> sc8-CPU converter, so 8-bit files would have to be converted in this script
CPU_FORMAT = "sc16"
SIGMF_DATATYPE = "ci16_le"  # SigMF's name for the CPU format as written to disk
SAMPLE_TYPE_DESC = "signed complex 16-bit samples (I+Q), little-endian"  # for the metadata
BYTES_PER_SAMPLE = 4  # 16 bits I + 16 bits Q = 32 bits = 4 bytes
'''
GPS L1 at the antenna is roughly -130 dBm. But at this power level the signal itself is irrelevant to gain-setting — it's 15-20 dB below the thermal noise floor regardless, so GNSS gain-setting is really about placing the noise floor correctly in the ADC's dynamic range, not the signal.

Noise floor in your 2 MHz bandwidth: kTB = -174 dBm/Hz + 10·log₁₀(2×10⁶) ≈ -111 dBm, before your LNA.

After your 20 dB LNA (ignoring its own noise figure, typically <1.5 dB for a decent GNSS LNA): noise floor at the USRP's input ≈ -91 dBm.
'''
DEFAULT_GAIN_DB = 25


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--args", default="addr=192.168.10.2", help="UHD device args (default: addr=192.168.10.2)")
    p.add_argument("--freq", type=float, nargs="+", default=[GPS_L1_FREQ, GPS_L5_FREQ],
                   help=f"center frequency per channel [Hz] (default: {GPS_L1_FREQ / 1e6:g}e6 "
                        f"{GPS_L5_FREQ / 1e6:g}e6, i.e. GPS L1 and L5)")
    p.add_argument("--iq-rate", type=float, nargs="+", default=None,
                   help="sample rate [S/s] at which the IQ samples are recorded, one value or one per channel "
                        f"(default: {DEFAULT_IQ_RATES[GPS_L1_FREQ]:g} for L1, {DEFAULT_IQ_RATES[GPS_L5_FREQ]:g} for L5)")
    p.add_argument("--gain", type=float, nargs="+", default=[DEFAULT_GAIN_DB],
                   help=f"gain [dB], one value or one per channel (default: {DEFAULT_GAIN_DB})")
    p.add_argument("--duration", type=float, default=300.0, help="recording length [s] (default: 300.0)")
    p.add_argument("--channels", type=int, nargs="+", default=[0, 1],
                   help="channel indices (0=RX0, 1=RX1) (default: 0 1)")
    p.add_argument("--clock-source", default="external",
                   help="10 MHz reference: external | internal (default: external)")
    p.add_argument("--time-source", default="internal",
                   help="PPS source: internal | external (default: internal)")
    p.add_argument("--out-dir", type=Path, default=Path("recordings"),
                   help="output directory (default: recordings)")
    p.add_argument("--name", default=None, help="file prefix (default: UTC timestamp)")
    args = p.parse_args()

    # replicate single-value freq/gain to all channels
    n_channels = len(args.channels)
    if len(args.freq) == 1:
        args.freq *= n_channels
    if len(args.gain) == 1:
        args.gain *= n_channels
    # no --iq-rate given: pick each channel's rate from the band it is tuned to
    if args.iq_rate is None:
        if any(f not in DEFAULT_IQ_RATES for f in args.freq):
            p.error("--iq-rate is required when a channel is tuned to neither GPS L1 nor L5")
        args.iq_rate = [DEFAULT_IQ_RATES[f] for f in args.freq]
    if len(args.iq_rate) == 1:
        args.iq_rate *= n_channels
    # check that freq/gain/iq-rate have the same number of values as channels
    if len(args.freq) != n_channels or len(args.gain) != n_channels or len(args.iq_rate) != n_channels:
        p.error("--freq, --gain and --iq-rate need either one value or one value per channel")

    return args


def apply_settings(usrp, args):
    """Apply clock/time sources, frontends, rate, frequency and gain; return the actual settings."""
    usrp.set_clock_source(args.clock_source)
    usrp.set_time_source(args.time_source)
    usrp.set_rx_subdev_spec(uhd.usrp.SubdevSpec(SUBDEV))

    # one setting per channel
    settings = []
    # apply setting for each channel and record the values
    for ch, freq, gain, iq_rate in zip(args.channels, args.freq, args.gain, args.iq_rate):
        usrp.set_rx_rate(iq_rate, ch)
        usrp.set_rx_freq(uhd.types.TuneRequest(freq), ch)
        usrp.set_rx_gain(gain, ch)
        rate_ch = usrp.get_rx_rate(ch)
        settings.append({
            "channel": ch,
            "iq_samp_rate": rate_ch,
            "freq": usrp.get_rx_freq(ch),
            "gain": usrp.get_rx_gain(ch),
            "antenna": usrp.get_rx_antenna(ch),
            "frontend": usrp.get_rx_subdev_name(ch),
            "otw_format": OTW_FORMAT,
            "cpu_format": CPU_FORMAT,
            "sample_type_desc": SAMPLE_TYPE_DESC,
            "bytes_per_sample": BYTES_PER_SAMPLE,
            "throughput_B/s": rate_ch * BYTES_PER_SAMPLE,  # B/s, this channel alone
        })
        if abs(rate_ch - iq_rate) > 1e-3:
            logger.warning(f"ch{ch} requested rate {iq_rate / 1e6} MS/s coerced to {rate_ch / 1e6} MS/s")

    # confirmation of what the USRP actually ended up configured at, for each channel (only useful for interactive runnings)
    for s in settings:
        logger.info(f"ch{s['channel']} ({s['frontend']}, {s['antenna']}): "
              f"freq={s['freq'] / 1e6:.6f} MHz  rate={s['iq_samp_rate'] / 1e6:.6f} MS/s  "
              f"gain={s['gain']:.1f} dB  otw={s['otw_format']}  cpu={s['cpu_format']}  throughput={s['throughput_B/s'] / 1e6:.1f} MB/s")

    total_throughput = sum(s["throughput_B/s"] for s in settings)  # B/s, all channels over the one link
    if total_throughput > LINK_LIMIT:
        logger.warning(f"{total_throughput / 1e6:.0f} MB/s exceeds what 1 GbE can carry; expect overflows")
    return settings


def wait_for_sensor(read, name):
    deadline = time.monotonic() + LOCK_TIMEOUT
    while not read().to_bool():
        if time.monotonic() > deadline:
            raise SystemExit(f"error: {name} not locked after {LOCK_TIMEOUT:.0f} s")
        time.sleep(0.1)
    logger.info(f"{name}: locked")


def wait_for_locks(usrp, args):
    '''
    The TwinRX daughterboard's RF downconversion LO is generated by its own synthesizer chip on the daughterboard, and yes — its reference input is derived from the same 10 MHz clock the motherboard distributes, whichever clock_source you selected (external or internal). So in that sense, the LO is driven off the reference you picked.

    But "driven by" isn't "locked to." Two separate PLLs are involved:

    - `ref_locked` checks the motherboard's primary clock-generation PLL: The X310 motherboard carries one clock-conditioner IC, the LMK04816 (Texas Instruments). "Motherboard's primary clock-generation PLL" is referring specifically to this chip. REF IN (or the internal reference, if clock_source=internal) feeds the LMK04816's PLL1 — a narrow-bandwidth loop that disciplines an internal 96 MHz VCXO to your reference. That disciplined VCXO then drives LMK04816's PLL2, a second internal loop with its own VCO, which is what actually generates the chip's output frequencies. (So it's two cascaded PLL stages inside one chip, not one.). From that VCO, the chip divides out multiple independent clock outputs, each to a dedicated physical pin:
        * CLKout6/7 and CLKout8/9 → the DAC and ADC master clock. It is your "sample clock" (200 MHz by default). 200 MHz is how fast the ADC itself samples (real-valued RF/IF samples). The I/Q rate you actually get (--rate, e.g. 2 MS/s) is much lower — the FPGA's digital downconverter (DDC) mixes and decimates that 200 MHz ADC stream down to your requested rate
        * CLKout2/3 (RX) and CLKout4/5 (TX) → a dedicated reference clock sent to the daughterboard slot — this is the signal that reaches the TwinRX board and feeds its own, separate LO synthesizer as that chip's reference input
        * CLKout0/1 → the FPGA's clock
        * CLKout10 → an optional buffered reference output (the REF OUT port)
    - `lo_locked` checks the daughterboard's own LO synthesizer — a separate PLL/VCO that has to achieve its own phase lock at the specific RF frequency you just tuned to via set_rx_freq. Its failure modes are different: it needs settling time after every retune, it can fail to lock if the requested frequency is awkward for that synthesizer, or it can fail to lock even with a perfectly good upstream reference, because locking is itself a dynamic process, not just "reference present = instantly locked."

    In summary: REF IN goes into this chip, and that one chip is the common ancestor of both the ADC/DAC master clock and the reference the daughterboard's LO synthesizer uses. They're siblings fed from the same source, via separate output pins of the same clock conditioner — which is precisely why their lock states (ref_locked for this chip; lo_locked for the daughterboard's own downstream synthesizer) can diverge: a good signal on one CLKout pin doesn't guarantee the chip on the other end of a different CLKout pin has finished locking to it yet.
    '''
    # wait for the external reference oscillator
    if args.clock_source == "external":
        wait_for_sensor(lambda: usrp.get_mboard_sensor("ref_locked", 0), "ref_locked")
    # wait for the local oscillators (LOs) for each channels
    for ch in args.channels:
        if "lo_locked" in usrp.get_rx_sensor_names(ch):
            wait_for_sensor(lambda: usrp.get_rx_sensor("lo_locked", ch), f"ch{ch} lo_locked")
        else:
            logger.warning(f"ch{ch} has no lo_locked sensor, skipping LO lock check")
    ''' Check time source (PPS)
    # CAVEAT: UHD doesn't expose a simple boolean sensor for PPS the way it does for ref_locked/lo_locked — there's no get_mboard_sensor("pps_locked", ...). The standard way UHD's own examples (benchmark_rate.cpp, rx_timed_samples.cpp) confirm a PPS edge is actually arriving is different: call usrp.get_time_last_pps() twice, a bit more than a second apart, and check the value actually advanced. If it hasn't moved, no PPS pulse reached the device — it's silently still free-running, and anything relying on time_spec-synchronized starts (which this script does, for multi-channel alignment) could be off.
    '''
    if args.time_source == "external":
        # No boolean "pps_locked" sensor exists in UHD; confirm a PPS edge is actually
        # arriving by checking get_time_last_pps() advances between two checks.
        deadline = time.monotonic() + LOCK_TIMEOUT
        last_pps = usrp.get_time_last_pps().get_real_secs()
        while usrp.get_time_last_pps().get_real_secs() == last_pps:
            if time.monotonic() > deadline:
                raise SystemExit(f"error: no PPS edge detected after {LOCK_TIMEOUT:.0f} s")
            time.sleep(0.2)
        logger.info("pps: detected")

def record_channel(streamer, iq_samp_rate, duration, path, stop):
    """Stream one channel to its .sigmf-data file until `duration` is reached or `stop` is set.
    Samples lost to an overflow are replaced by zeros, so a sample's position in the file always
    matches its time (and both channels stay aligned). Returns (samples written, zeros included;
    lost-sample gaps as (sample index, count) pairs)."""
    # init a blank empty metadata container
    md = uhd.types.RXMetadata()

    # total number of I/Q samples the recording should stop at — the stopping condition for the receive loop.
    total_iq_samps = round(duration * iq_samp_rate)
    n_written_iq_samps = 0
    # number of samples that were lost
    lost_samps = []

    # the timout handed to streamer.recv() is the maximum time to wait for a packet of samples to arrive.
    timeout = START_DELAY + 1.0

    # Upper bound on how many samples a single packet carries, determined by the transport's packet size (for the X310 over Ethernet, that's bounded by the UDP/Ethernet MTU divided by the sample size). recv() fills the buffer across several packets, so the buffer holds RECV_CHUNK seconds instead, keeping the Python loop rate low at high sample rates
    samps_per_packet = streamer.get_max_num_samps()
    n_buf_samps = max(samps_per_packet, round(RECV_CHUNK * iq_samp_rate))
    logger.info(f"{path.name}: {iq_samp_rate / 1e6:g} MS/s, {samps_per_packet:,} samples per network packet, "
                f"each read (recv) returns one batch of up to {n_buf_samps:,} samples "
                f"({n_buf_samps / iq_samp_rate * 1e3:g} ms, ~{n_buf_samps // samps_per_packet} packets), "
                f"{total_iq_samps:,} samples in total")
    # create an empty buffer array to hold the received samples (one row: this streamer's only channel)
    # (np.uint32 = one 4-byte sc16 I/Q pair per element, written to disk as-is)
    buf = np.empty((1, n_buf_samps), dtype=np.uint32)
    zeros = np.zeros(n_buf_samps, dtype=np.uint32)  # written in place of lost samples
    with open(path, "wb") as f:
        try:
            n_reads = 0  # recv() calls that returned samples, for the log
            while n_written_iq_samps < total_iq_samps and not stop.is_set():
                # Pulls one batch of received I/Q samples from the USRP into buf, waiting up to timeout seconds if none have arrived yet, and returns how many samples it actually got this call (n_iq_samp_this_call).
                n_iq_samp_this_call = streamer.recv(buf, md, timeout)
                timeout = 0.5
                # check error (if any)
                err = md.error_code
                if err == uhd.types.RXMetadataErrorCode.overflow:
                    continue
                if err != uhd.types.RXMetadataErrorCode.none:
                    raise RuntimeError(f"receive error: {md.strerror()}")
                if n_iq_samp_this_call == 0:
                    continue
                n_reads += 1

                # Lost-sample check: does this batch continue the file directly, or is there a hole in between?
                # It compares the time right after the file's last sample with the time of the batch's first
                # sample, both expressed as sample indices (time x rate):
                #   - n_written_iq_samps: samples in the file so far = index where the next sample should go
                #   - n_expected_iq_samps: index where this batch's first sample belongs, from its USRP timestamp
                # Equal -> no loss. Batch ahead of the file -> the samples in between were dropped (overflow).
                # E.g. at 5 MS/s, the file holds 49,200,000 samples (it ends at t = 9.84 s) but the batch is
                # stamped t = 10.00 s (index 50,000,000): 800,000 samples (0.16 s) are missing.
                if md.has_time_spec:
                    # index (from the start of the recording) of this batch's first sample, per the USRP's clock
                    n_expected_iq_samps = min(round((md.time_spec.get_real_secs() - START_DELAY) * iq_samp_rate),
                                              total_iq_samps)
                    # Fill the hole with zeros, so the batch lands at the index matching its timestamp (keeping
                    # file position = time, and both channels aligned). This also makes n_written_iq_samps catch
                    # up with n_expected_iq_samps, so this gap is counted only once, not again on every later read
                    n_lost = n_expected_iq_samps - n_written_iq_samps
                    if n_lost > 0:
                        lost_samps.append((n_written_iq_samps, n_lost))
                        # t = time since the recording started, on the USRP's clock
                        logger.warning(f"{path.name}: packets dropped before read #{n_reads}:\n"
                                       f"    file so far ends at sample {n_written_iq_samps:,}, "
                                       f"with timestamp {n_written_iq_samps / iq_samp_rate:.6f} s\n"
                                       f"    this batch starts at sample {n_expected_iq_samps:,}, "
                                       f"with timestamp {n_expected_iq_samps / iq_samp_rate:.6f} s\n"
                                       f"    -> {n_lost:,} samples ({n_lost / iq_samp_rate * 1e3:.1f} ms, "
                                       f"{n_lost * BYTES_PER_SAMPLE / 1e6:.1f} MB, "
                                       f"~{n_lost / samps_per_packet:.0f} packets) never arrived; zero-filled")
                        for start in range(0, n_lost, n_buf_samps):
                            zeros[:min(n_buf_samps, n_lost - start)].tofile(f)
                        n_written_iq_samps += n_lost

                n_iq_samp_to_write = min(n_iq_samp_this_call, total_iq_samps - n_written_iq_samps)
                buf[0, :n_iq_samp_to_write].tofile(f)
                n_written_iq_samps += n_iq_samp_to_write
        finally:
            streamer.issue_stream_cmd(uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont))
            while streamer.recv(buf, md, 0.1):  # drain what is still in flight
                pass
    return n_written_iq_samps, lost_samps


def record(usrp, args, settings, paths):
    """Stream all channels to their .sigmf-data files. Returns ([(samples written, gap annotations)]
    per channel, start time).

    Each channel gets its own streamer, received in its own thread: a single multi-channel
    streamer hands back the same number of samples per channel per recv(), so it can't carry
    channels running at different sample rates (e.g. L1 at 4 MS/s and L5 at 20 MS/s). All
    streamers are started by the same timed command, so they still begin on the same instant."""
    streamers = []
    for s in settings:
        stream_args = uhd.usrp.StreamArgs(CPU_FORMAT, OTW_FORMAT)
        stream_args.channels = [s["channel"]]
        # create a streaming handle session to the USRP that stays open indefinitely until you explicitly tell it to stop
        streamers.append(usrp.get_rx_stream(stream_args))

    # resets the USRP's own internal hardware time register to zero
    usrp.set_time_now(uhd.types.TimeSpec(0.0))
    # create a StreamCMD object named cmd, a command describing what to do
    cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
    cmd.stream_now = False
    # "start streaming when my internal clock reaches 0.5s." Zeroing the device clock first, in usrp.set_time_now(uhd.types.TimeSpec(0.0)), is what makes "0.5s" a well-defined instant to schedule against
    cmd.time_spec = uhd.types.TimeSpec(START_DELAY)
    #  issue_stream_cmd(cmd) is non-blocking: it just hands this command to the device and returns immediately. The actual triggering — "now begin streaming samples" — happens autonomously inside the USRP's own firmware/FPGA, which watches its internal clock (zeroed lines ago) and starts emitting samples the instant that clock crosses 0.5s, with no further involvement from the host/Python side.
    for streamer in streamers:
        streamer.issue_stream_cmd(cmd)
    # save the UTC timestamp of when the recording started just for the record, to be used outside this function
    start_utc = datetime.now(timezone.utc) + timedelta(seconds=START_DELAY)

    # set by the main thread (Ctrl+C) or when any channel fails, so every channel stops together
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=len(streamers)) as pool:
        futures = [pool.submit(record_channel, streamer, s["iq_samp_rate"], args.duration, path, stop)
                   for streamer, s, path in zip(streamers, settings, paths)]
        try:
            # poll instead of blocking on result(), so Ctrl+C reaches the main thread
            while not all(f.done() for f in futures):
                if any(f.done() and f.exception() for f in futures):
                    stop.set()
                time.sleep(0.2)
        except KeyboardInterrupt:
            logger.warning("interrupted, finalizing files")
            stop.set()
    return [f.result() for f in futures], start_utc


def write_meta(path, data_path, s, args, start_utc, lost_samps, n_written_iq_samps):
    """Build this channel's metadata dict and write it as a SigMF Recording's .sigmf-meta
    file. Goes through SigMFFile (not a raw JSON dump) so the result is schema-validated
    before being written, instead of just assumed correct."""
    meta = {
        "global": {
            "core:datatype": SIGMF_DATATYPE,
            "core:sample_rate": s["iq_samp_rate"],
            "core:sample_type": s["cpu_format"],
            "x310:otw_format": s["otw_format"],
            "core:sample_type_desc": s["sample_type_desc"],
            "core:bytes_per_sample": s["bytes_per_sample"],
            "core:version": "1.0.0",
            "core:num_channels": 1,
            "core:motherboard": "USRP X310",
            "x310:daughterboard": s['frontend'],
            "core:recorder": "UHD Python API",
            "core:description": f"{args.args}, channel {s['channel']}",
            "core:extensions": [{"name": "x310", "version": "0.1.0", "optional": True}],
            "x310:gain_db": s["gain"],
            "x310:antenna": s["antenna"],
            "x310:clock_source": args.clock_source,
            "x310:time_source": args.time_source,
            "x310:start_delay": START_DELAY,
            "x310:lost_samples": sum(n for _, n in lost_samps),
            "x310:written_samples": n_written_iq_samps,
        },
        "captures": [{
            "core:sample_start": 0,
            "core:frequency": s["freq"],
            "core:datetime": start_utc.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        }],
        "annotations": [
            {"core:sample_start": start, "core:sample_count": lost,
             "core:comment": "overflow: lost samples, zero-filled", "x310:lost_samples": lost}
            for start, lost in lost_samps
        ],
    }
    sigmf_file = SigMFFile(metadata=meta, data_file=str(data_path))
    sigmf_file.validate()
    sigmf_file.tofile(path, toarchive=False)


def build_archive(name, out_dir, meta_paths):
    """Bundle this session's Recordings (one per channel) into a single SigMF Archive:
    Archive (<name>.sigmf, a tar file) > Collection (<name>.sigmf-collection, linking every
    channel's Recording by name + sha512 hash) > Recordings (each channel's .sigmf-meta +
    .sigmf-data pair) -- matching the SigMF spec's Archive/Collection/Recording structure.
    The loose per-channel files are removed afterward, leaving just the one archive file."""
    collection = SigMFCollection(metafiles=[p.name for p in meta_paths], base_path=out_dir)
    collection_path = out_dir / f"{name}.sigmf-collection"
    collection.tofile(collection_path, overwrite=True)

    data_paths = [p.with_suffix(".sigmf-data") for p in meta_paths]
    members = [collection_path, *meta_paths, *data_paths]

    archive_path = out_dir / f"{name}.sigmf"
    with tarfile.open(archive_path, "w") as tar:
        for member in members:
            # one subdirectory named after the archive holding everything flat, same
            # convention SigMF's own single-recording SigMFArchive uses internally
            tar.add(member, arcname=f"{name}/{member.name}")

    for member in members:
        member.unlink()
    return archive_path


def check_otw_format():
    """Refuse to record with an OTW format the X310 doesn't send: UHD accepts e.g. sc8 without any error,
    but the X310 keeps sending sc16, so every recorded sample would be silently misread."""
    if OTW_FORMAT not in X310_OTW_FORMATS:
        logger.error(f"OTW_FORMAT = {OTW_FORMAT!r}, but the X310 only sends {', '.join(sorted(X310_OTW_FORMATS))} "
                     f"over the wire. UHD would accept {OTW_FORMAT!r} silently and misread every 16-bit sample "
                     f"(e.g. as 8-bit I/Q pairs), producing corrupted recordings; set OTW_FORMAT = 'sc16'")
        raise SystemExit(1)


def main():
    args = parse_args()
    check_otw_format()
    # set the recording name to the user-specified name or a UTC timestamp string if not specified
    name = args.name or datetime.now(timezone.utc).strftime("x310_%Y%m%dT%H%M%SZ")
    # create the output directory
    args.out_dir.mkdir(parents=True, exist_ok=True)

    '''
    It opens a connection to the USRP and gives you a handle to control it: uhd.usrp.MultiUSRP(...) is UHD's main Python class for talking to a USRP device, and args.args (the --args device string, e.g. "addr=192.168.10.2") tells it which device to connect to and how — here, over Ethernet at that IP.
    '''
    usrp = uhd.usrp.MultiUSRP(args.args)
    # Apply the settings to the USRP and get the actual settings back
    settings = apply_settings(usrp, args)
    # Wait for the USRP to lock its reference clock (external 10 MHz) and the local oscillators (LOs) for each channel
    wait_for_locks(usrp, args)

    logger.info(f"recording {args.duration} s ...")
    
    # one .sigmf-data path per channel 
    basenames = [args.out_dir / f"{name}_ch{s['channel']}" for s in settings]
    # add the extension
    data_paths = [b.parent / f"{b.name}.sigmf-data" for b in basenames]
    
    # start the record
    results, start_utc = record(usrp, args, settings, data_paths)

    # for each channel, write the metadata file and print a summary of what was recorded
    meta_paths = []
    for setting, basename, data_path, (n_written_iq_samps, lost_samps) in zip(settings, basenames, data_paths, results):
        meta_path = basename.parent / f"{basename.name}.sigmf-meta"
        write_meta(meta_path, data_path, setting, args, start_utc, lost_samps, n_written_iq_samps)
        meta_paths.append(meta_path)
        logger.info(f"{data_path}: {n_written_iq_samps} samples ({data_path.stat().st_size / 1e6:.1f} MB), "
                    f"overflows: {len(lost_samps)} ({sum(n for _, n in lost_samps)} samples lost)")

    # bundle every channel's Recording into one SigMF Archive (Archive > Collection > Recordings)
    archive_path = build_archive(name, args.out_dir, meta_paths)
    logger.info(f"archive: {archive_path} ({archive_path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
