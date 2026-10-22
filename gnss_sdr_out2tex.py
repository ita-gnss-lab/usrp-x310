"""Turn one recording's GNSS-SDR run (run_gnss_sdr.sh --report) into figures and tables for report/main.tex.

One block of evidence per receiver stage, in the order of the report's "Validating the recordings with GNSS-SDR"
section, written to <out>/results.tex (\\input under the recording's metadata table) plus PNG figures:
  1. Signal source        PSD + I/Q histogram per channel (from the raw recording); table of sample statistics
  2. Signal conditioner   table: recorded format/rate -> what gnss-sdr processes (format, rate, upsampling)
  3. Acquisition          delay-Doppler map of the strongest detection per band; table of attempts/detections
  4. Tracking             C/N0 and Doppler vs time per satellite; prompt I/Q scatter; table per satellite
  5. Telemetry decoder    timeline of valid navigation-message time; table: messages, ephemeris, first TOW
  6. Observables          availability per satellite; P_band - P_L1 vs time; table (compare_pseudoranges.py stats)
  7. PVT                  ENU scatter + time series about the surveyed antenna, skyplot; table of position errors
Every element is optional: a missing GNSS-SDR output becomes a "not produced" line instead of a LaTeX error, so
the report always compiles and shows how far the receiver got.

Example:
  uv run gnss_sdr_out2tex.py gnss_sdr_output/x310_20261021T193000Z --recording recordings/x310_20261021T193000Z.sigmf
"""

import argparse
import csv
import json
import re
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path

import h5py
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from compare_pseudoranges import load_observables  # noqa: E402
from gnss_spp import enu_errors, load_lnav_ephemeris, solve  # noqa: E402
from report_lib import tex_escape  # noqa: E402

ANTENNA_LLH = (-23.209433, -45.874875, 596.42)  # surveyed antenna position (lat, lon [deg], h [m]), GNSS lab ITA
BAND_LABEL = {"1C": "L1 C/A", "2S": "L2C", "L5": "L5"}
BAND_FREQ_MHZ = {"1C": 1575.42, "2S": 1227.60, "L5": 1176.45}
CHIP_RATE = {"1C": 1.023e6, "2S": 1.023e6, "L5": 10.23e6}
LOCK_OK = 0.8  # carrier lock test (cos 2*phase error) above which the PLL is counted as phase-locked
SOURCE_SECONDS = 0.5  # raw samples analysed per channel for the signal-source stage
SOURCE_OFFSET_S = 10.0  # ... starting this far into the recording
FIG_W = 6.3  # inches, \linewidth of the article class
plt.rcParams.update({"font.size": 8, "axes.grid": True, "grid.alpha": 0.3, "savefig.bbox": "tight"})


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run_dir", type=Path, help="gnss_sdr_output/<name> of a run_gnss_sdr.sh --report run")
    p.add_argument("--recording", type=Path, required=True, help="the .sigmf archive that was processed")
    p.add_argument("--out", type=Path, default=None, help="output directory (default: report/gnss_sdr/<name>)")
    p.add_argument("--tex-root", type=Path, default=Path("report"),
                   help="directory main.tex is compiled in; figure paths in results.tex are relative to it")
    return p.parse_args()


# ---------------------------------------------------------------------------------------------------- helpers
def load_mat(path):
    """All datasets of a gnss-sdr .mat (MAT v7.3 = HDF5) as flat numpy arrays."""
    with h5py.File(path, "r") as f:
        return {k: np.asarray(f[k]).ravel() for k in f if isinstance(f[k], h5py.Dataset)}


def conf_values(run_dir):
    conf = next(run_dir.glob("*.conf"))
    values = {}
    for line in conf.read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)=(.*?)\s*$", line)
        if m:
            values[m.group(1)] = m.group(2)  # last occurrence wins, as in gnss-sdr
    return conf.name, values


def bands_of(values):
    """[(signal code, first channel, n channels)] in gnss-sdr's channel order (1C, 2S, L5)."""
    out, first = [], 0
    for sig in ("1C", "2S", "L5"):
        n = int(values.get(f"Channels_{sig}.count", 0))
        if n:
            out.append((sig, first, n))
            first += n
    return out


def band_of_channel(bands, ch):
    for sig, first, n in bands:
        if first <= ch < first + n:
            return sig
    return None


CONF_NAME_FILE = "gnss_sdr_conf.txt"  # name of the gnss-sdr configuration the recording was processed with
FIG_DPI = 200  # raster figures: vector PDFs of plots with ~10^4-10^5 points made the report too heavy to render


def fig_path(out, name):
    return out / f"{name}.png"


def savefig(fig, out, name, tex):
    fig.savefig(fig_path(out, name), dpi=FIG_DPI)
    plt.close(fig)
    return fig_path(out, name).relative_to(tex.root).as_posix()


class Tex:
    """Collects results.tex: one subsubsection per stage, with its figures and tables."""

    def __init__(self, root, name):
        self.root, self.name, self.lines = root, name, []

    def para(self, title, text=""):
        """One receiver stage: a \\subsubsection under the recording's \\subsection (title without its final dot)."""
        self.lines += [f"\\subsubsection{{{title.rstrip('.')}}}", text, ""]

    def text(self, text):
        self.lines += [text, ""]

    def missing(self, what):
        self.lines += [f"\\textit{{Not produced: {what}.}}", ""]

    def figure(self, rel, caption, width=1.0):
        self.lines += [r"\begin{figure}[H]", r"  \centering",
                       f"  \\includegraphics[width={width}\\linewidth]{{{rel}}}",
                       f"  \\caption{{{caption}}}", r"\end{figure}", ""]

    def table(self, header, rows, caption, align=None, span_rows=()):
        """span_rows: (label, value) rows whose value spans every data column, placed right under the header."""
        align = align or "l" + "r" * (len(header) - 1)
        # shrunk to the text width only when wider than it (\\width is the tabular's natural width)
        self.lines += [r"\begin{table}[H]", r"  \centering", r"  \footnotesize",
                       r"  \resizebox{\ifdim\width>\linewidth\linewidth\else\width\fi}{!}{%",
                       f"  \\begin{{tabular}}{{{align}}}", r"    \hline",
                       "    " + " & ".join(f"\\textbf{{{h}}}" for h in header) + r" \\", r"    \hline"]
        self.lines += [f"    {label} & \\multicolumn{{{len(header) - 1}}}{{c}}{{{value}}} \\\\" for label, value in span_rows]
        self.lines += ["    " + " & ".join(str(c) for c in row) + r" \\" for row in rows]
        self.lines += [r"    \hline", r"  \end{tabular}}", f"  \\caption{{{caption}}}", r"\end{table}", ""]

    def table_t(self, header, rows, caption, span_rows=()):
        """table() transposed: one row per quantity (header[1:]), one column per item (rows' first cells), for
        tables with few items but many quantities, which would be too wide the other way round."""
        new_header = [header[0]] + [str(r[0]) for r in rows]
        new_rows = [[header[i]] + [r[i] for r in rows] for i in range(1, len(header))]
        self.table(new_header, new_rows, caption, align="l" + "c" * len(rows), span_rows=span_rows)

    def write(self, path):
        path.write_text("\n".join(self.lines) + "\n")


# ----------------------------------------------------------------------------------------- 1. signal source
def read_archive_meta(archive):
    metas = {}
    with tarfile.open(archive) as tar:
        for m in tar.getmembers():
            mm = re.search(r"_ch(\d+)\.sigmf-meta$", m.name)
            if mm:
                metas[int(mm.group(1))] = json.load(tar.extractfile(m))
    return metas


def read_samples(archive, ch, meta):
    """SOURCE_SECONDS of complex samples of channel ch, in the file's integer units."""
    g = meta["global"]
    dtype = {"ci8": np.int8, "ci16_le": np.int16}[g["core:datatype"]]
    fs = g["core:sample_rate"]
    n = int(SOURCE_SECONDS * fs)
    offset = int(SOURCE_OFFSET_S * fs) * 2 * np.dtype(dtype).itemsize
    with tarfile.open(archive) as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith(f"_ch{ch}.sigmf-data"))
        f = tar.extractfile(member)
        f.seek(min(offset, max(member.size - 2 * n * np.dtype(dtype).itemsize, 0)))
        raw = np.frombuffer(f.read(2 * n * np.dtype(dtype).itemsize), dtype=dtype).astype(np.float32)
    return raw[0::2] + 1j * raw[1::2], fs, g


def stage_source(tex, out, archive, metas, bands):
    tex.para("Signal source.", "Raw I/Q samples of each channel, as recorded "
             f"({SOURCE_SECONDS:g}~s analysed, from {SOURCE_OFFSET_S:g}~s into the recording).")
    chans = sorted(metas)
    fig, axes = plt.subplots(len(chans), 2, figsize=(FIG_W, 2.0 * len(chans)), squeeze=False)
    rows = []
    for i, ch in enumerate(chans):
        x, fs, g = read_samples(archive, ch, metas[ch])
        full = {"ci8": 127, "ci16_le": 32767}[g["core:datatype"]]
        nfft = 4096
        segs = x[: len(x) // nfft * nfft].reshape(-1, nfft) * np.hanning(nfft)
        psd = np.fft.fftshift(np.mean(np.abs(np.fft.fft(segs, axis=1)) ** 2, axis=0)) / (fs * np.sum(np.hanning(nfft) ** 2))
        f_mhz = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / fs)) / 1e6
        freq = metas[ch]["captures"][0]["core:frequency"] / 1e6
        ax = axes[i, 0]
        ax.plot(f_mhz, 10 * np.log10(psd + 1e-30), lw=0.6)
        ax.set(xlabel=f"frequency offset from {freq:.2f} MHz [MHz]", ylabel="PSD [dB/Hz, file units]",
               title=f"ch{ch} power spectral density")
        ax = axes[i, 1]
        v = np.concatenate([x.real, x.imag])
        sd = v.std()
        lim = min(full, 5 * sd)
        bins = np.arange(-np.ceil(lim), np.ceil(lim) + 1) if g["core:datatype"] == "ci8" else 100
        ax.hist(x.real, bins=bins, density=True, alpha=0.6, label="I")
        ax.hist(x.imag, bins=bins, density=True, alpha=0.6, label="Q")
        t = np.linspace(-lim, lim, 400)
        ax.plot(t, np.exp(-t ** 2 / (2 * sd ** 2)) / (sd * np.sqrt(2 * np.pi)), "k", lw=0.8, label="Gaussian")
        ax.set(xlabel="sample value", ylabel="density", title=f"ch{ch} I/Q histogram", xlim=(-lim, lim))
        ax.legend(fontsize=6)
        clip = np.mean(np.abs(v) >= full) * 100
        rows.append([f"ch{ch}", f"{freq:.2f}", f"{fs / 1e6:g}", tex_escape(g["core:datatype"]),
                     f"{g['x310:gain_db']:g}", f"{x.real.std():.1f} / {x.imag.std():.1f}",
                     f"{np.mean(x.real):.2f} / {np.mean(x.imag):.2f}", f"{clip:.3f}",
                     f"{g.get('x310:lost_samples', 0):,}"])
    fig.tight_layout()
    rel = savefig(fig, out, "source", tex)
    tex.figure(rel, "Signal source: power spectral density (the receiver passband over a flat thermal-noise floor; "
               "GNSS signals are below the noise, so only interference would stand out) and I/Q histograms "
               "(Gaussian noise, centred, not clipped).")
    tex.table_t(["Channel", "Frequency [MHz]", "Rate [MS/s]", "Format", "Gain [dB]", "Std I / Q", "Mean I / Q",
               "Clipped [\\%]", "Lost samples"], rows, "Signal source: sample statistics per channel.")


# ------------------------------------------------------------------------------------ 2. signal conditioner
def stage_conditioner(tex, values, metas, bands):
    tex.para("Signal conditioner.", "How each recorded channel is turned into the complex baseband stream the "
             "channels process.")
    internal = float(values.get("GNSS-SDR.internal_fs_sps", 0))
    rows = []
    for i, (sig, _, _) in enumerate(bands):
        g = metas[i]["global"]
        fs = g["core:sample_rate"]
        item = values.get("SignalSource.item_type", values.get(f"SignalSource{i}.item_type", "?"))
        k = round(internal / fs) if fs else 0
        resamp = values.get(f"Resampler{i}.implementation", "?")
        up = f"$\\times${k} zero-order hold (offline)" if k > 1 and resamp == "Pass_Through" else resamp
        rows.append([f"ch{i} ({BAND_LABEL[sig]})", tex_escape(g["core:datatype"]), tex_escape(item),
                     tex_escape(values.get(f"DataTypeAdapter{i}.implementation", "?")),
                     tex_escape(values.get(f"InputFilter{i}.implementation", "?")), tex_escape(up),
                     f"{fs / 1e6:g} $\\to$ {internal / 1e6:g}"])
    tex.table_t(["Channel", "File format", "Source item type", "Data type adapter", "Input filter", "Resampling",
               "Rate [MS/s]"], rows, "Signal conditioner: recorded format and rate to gnss-sdr's internal "
               "\\texttt{gr\\_complex} stream; both bands are read in lockstep by one "
               "\\texttt{Multichannel\\_File\\_Signal\\_Source}.")


# ------------------------------------------------------------------------------------------ 3. acquisition
def stage_acquisition(tex, out, run_dir, bands, fs):
    tex.para("Acquisition.", "Two-dimensional search over code delay and Doppler (one channel per band dumped; "
             "the others behave alike).")
    summary = run_dir / "acq_summary.csv"
    if not summary.exists():
        tex.missing("acquisition dumps (acq\\_summary.csv)")
        return
    with open(summary) as f:
        rows = list(csv.DictReader(f))
    grids = sorted(run_dir.glob("acq_*.mat"))
    shown = []
    for sig, _, _ in bands:
        cands = []
        for p in grids:
            if p.name.startswith(f"acq_{sig}_"):
                d = load_mat(p)
                cands.append((float(d["test_statistic"][0]), p))
        if not cands:
            continue
        _, p = max(cands)
        with h5py.File(p, "r") as f:
            grid = np.asarray(f["acq_grid"])  # (n_doppler, n_delay)
            d = {k: float(np.asarray(f[k]).ravel()[0]) for k in f if k != "acq_grid"}
        n_dop, n_del = grid.shape
        dopp = -d["doppler_max"] + d["doppler_step"] * np.arange(n_dop)
        chips = np.arange(n_del) / fs * CHIP_RATE[sig]
        # 2-D map (a 3-D surface would need mpl_toolkits, which the system matplotlib shadows in this venv) plus the
        # cut through the peak along the code delay
        i_dop, i_del = np.unravel_index(np.argmax(grid), grid.shape)
        norm = grid / np.median(grid)  # power relative to the noise floor
        # the peak is ~2 samples wide in a 1-ms code period of thousands: zoom the map to +-5 chips around it
        win = int(5 * fs / CHIP_RATE[sig])
        lo, hi = max(i_del - win, 0), min(i_del + win + 1, n_del)
        fig, axes = plt.subplots(1, 2, figsize=(FIG_W, 2.4), gridspec_kw={"width_ratios": [1.3, 1]})
        im = axes[0].imshow(norm[:, lo:hi], aspect="auto", origin="lower", cmap="viridis", interpolation="nearest",
                            extent=[chips[lo], chips[hi - 1], dopp[0] / 1e3, dopp[-1] / 1e3])
        fig.colorbar(im, ax=axes[0], label="power / noise floor")
        axes[0].set(xlabel="code delay [chips]", ylabel="Doppler [kHz]",
                    title=f"{BAND_LABEL[sig]} PRN {int(d['PRN'])}: delay-Doppler map (zoom)")
        axes[1].plot(chips, norm[i_dop], lw=0.5)
        axes[1].set(xlabel="code delay [chips]", ylabel="power / noise floor",
                    title=f"whole code period at {dopp[i_dop]:.0f} Hz")
        fig.tight_layout()
        shown.append((savefig(fig, out, f"acq_{sig}", tex), sig, d))
    for rel, sig, d in shown:
        tex.figure(rel, f"Acquisition, {BAND_LABEL[sig]}: delay--Doppler search of PRN {int(d['PRN'])}; "
                   f"one sharp peak (test statistic {d['test_statistic']:.0f} vs threshold {d['threshold']:.0f}) at "
                   f"{d['acq_doppler_hz']:.0f}~Hz and {d['acq_delay_samples'] / fs * CHIP_RATE[sig]:.1f}~chips.")
    table = []
    for sig, _, _ in bands:
        r = [x for x in rows if x["band"] == sig]
        if not r:  # run_gnss_sdr.sh skips the dump when one attempt's grid is too large (L2C: 320 MB)
            table.append([BAND_LABEL[sig], "\\multicolumn{4}{l}{not dumped (search grid too large); acquired "
                          "satellites: see the tracking table}"])
            continue
        det = [x for x in r if x["detected"] == "1"]
        prns = sorted({int(x["prn"]) for x in det})
        best = max(det, key=lambda x: float(x["test_statistic"])) if det else None
        table.append([BAND_LABEL[sig], len(r), len(det), ", ".join(map(str, prns)) or "--",
                      f"{float(best['test_statistic']) / float(best['threshold']):.1f}" if best else "--"])
    tex.table(["Band", "Attempts", "Detections", "Detected PRNs", "Best statistic / threshold"], table,
              "Acquisition: search attempts of the dumped channel. Attempts on satellites that are not in view "
              "(or, for L5/L2C, do not broadcast that signal) are rejected below the threshold.")


# ---------------------------------------------------------------------------------------------- 4. tracking
def losses_of_lock(run_dir):
    counts = {}
    info = run_dir / "gnss-sdr.INFO"
    if info.exists():
        for m in re.finditer(r"Loss of lock in channel (\d+)", info.read_text(errors="replace")):
            counts[int(m.group(1))] = counts.get(int(m.group(1)), 0) + 1
    return counts


def stage_tracking(tex, out, run_dir, bands, fs):
    tex.para("Tracking.", "Code (DLL) and carrier (PLL) loops per satellite.")
    trk = {}
    for p in run_dir.glob("trk_*_*.mat"):
        m = re.match(r"trk_(\w\w)_(\d+)\.mat$", p.name)
        if m:
            d = load_mat(p)
            if d.get("PRN") is not None and d["PRN"].size > 100:
                trk[(m.group(1), int(m.group(2)))] = d
    if not trk:
        tex.missing("tracking dumps")
        return
    losses = losses_of_lock(run_dir)
    fig, axes = plt.subplots(2, len(bands), figsize=(FIG_W, 3.8), squeeze=False, sharex=True)
    rows, best = [], {}
    for j, (sig, _, _) in enumerate(bands):
        for (s, ch), d in sorted(trk.items()):
            if s != sig:
                continue
            t = d["PRN_start_sample_count"] / fs
            for prn in np.unique(d["PRN"][d["PRN"] > 0]).astype(int):
                m = d["PRN"] == prn
                if m.sum() < 100:
                    continue
                # plot only while phase-locked: the pull-in transient and tracks being lost (C/N0 estimates down
                # to -100 dB-Hz, Doppler running away) would hide the locked part's scale
                ok = m & (d["carrier_lock_test"] > LOCK_OK) & (d["CN0_SNV_dB_Hz"] > 20)  # C/N0 estimator warm-up
                dec = max(1, ok.sum() // 2000)
                tt = np.where(ok, t, np.nan)  # NaN breaks the line across unlocked gaps
                axes[0, j].plot(tt[m][::dec], d["CN0_SNV_dB_Hz"][m][::dec], lw=0.6, label=f"{prn}")
                axes[1, j].plot(tt[m][::dec], d["carrier_doppler_hz"][m][::dec] / 1e3, lw=0.6)
                cn0 = np.median(d["CN0_SNV_dB_Hz"][m])
                locked = np.mean(d["carrier_lock_test"][m] > LOCK_OK) * 100
                rows.append([BAND_LABEL[sig], ch, prn, f"{cn0:.1f}", f"{locked:.0f}", losses.get(ch, 0),
                             f"{m.sum() * np.median(np.diff(t[m])):.0f}" if m.sum() > 1 else "--"])
                if cn0 > best.get(sig, (0,))[0]:
                    best[sig] = (cn0, prn, d, m)
        axes[0, j].set(title=BAND_LABEL[sig], ylabel="C/N$_0$ [dB-Hz]")
        axes[0, j].legend(fontsize=5, ncol=4, title="PRN", title_fontsize=5)
        axes[1, j].set(xlabel="time [s]", ylabel="Doppler [kHz]")
    fig.tight_layout()
    tex.figure(savefig(fig, out, "tracking", tex), "Tracking: carrier-to-noise density ratio and carrier Doppler "
               "per satellite, while phase-locked (smooth Doppler curves = loops following the real satellite motion).")
    fig, axes = plt.subplots(1, len(best), figsize=(FIG_W, 2.6), squeeze=False)
    for j, (sig, (cn0, prn, d, m)) in enumerate(sorted(best.items(), key=lambda kv: [b[0] for b in bands].index(kv[0]))):
        ii, qq = d["Prompt_I"][m][-5000:], d["Prompt_Q"][m][-5000:]
        axes[0, j].plot(ii, qq, ".", ms=1)
        lim = 1.1 * np.max(np.abs(np.concatenate([ii, qq])))
        axes[0, j].set(xlabel="prompt I", ylabel="prompt Q", title=f"{BAND_LABEL[sig]} PRN {prn}",
                       xlim=(-lim, lim), ylim=(-lim, lim), aspect="equal")
    fig.tight_layout()
    tex.figure(savefig(fig, out, "tracking_iq", tex), "Tracking: prompt correlator outputs of the strongest "
               "satellite per band. Two tight clusters on one axis (the $\\pm1$ data bits; GNSS-SDR's L5 tracking puts "
               "the data component on the Q axis) mean the PLL is phase-locked; a ring would mean it is not.")
    tex.table(["Band", "Ch.", "PRN", "Median C/N$_0$ [dB-Hz]", "Phase-locked [\\%]", "Losses of lock",
               "Tracked [s]"], rows, f"Tracking: per channel and satellite (phase-locked: carrier lock test "
               f"$> {LOCK_OK}$).")


# ------------------------------------------------------------------------------------- 5. telemetry decoder
def ephemeris_prns(path):
    if not path.exists():
        return set()
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return set()
    return {int(i.find("first").text) for i in root.iter("item") if i.find("first") is not None}


def nav_message_counts(run_dir):
    """{(band, PRN): number of decoded navigation messages} from gnss-sdr's console output."""
    counts = {}
    stdout = run_dir / "gnss-sdr.stdout"
    if not stdout.exists():
        return counts
    text = re.sub(r"\x1b\[[0-9;]*m", "", stdout.read_text(errors="replace"))
    for m in re.finditer(r"New GPS (NAV|L5 CNAV|L2C? ?CNAV|CNAV) message received in channel \d+: .*?satellite GPS PRN (\d+)", text):
        band = "1C" if m.group(1) == "NAV" else ("L5" if "L5" in m.group(1) else "2S")
        counts[(band, int(m.group(2)))] = counts.get((band, int(m.group(2))), 0) + 1
    return counts


def stage_telemetry(tex, out, run_dir, bands, fs):
    tex.para("Telemetry decoder.", "Navigation-message decoding: bit/frame synchronization, parity/CRC, the "
             "ephemeris, and the satellite transmit time (time of week) of every measurement.")
    tlm = {}
    for p in run_dir.glob("tlm_*_*.mat"):
        m = re.match(r"tlm_(\w\w)_(\d+)\.mat$", p.name)
        if m:
            d = load_mat(p)
            if d.get("PRN") is not None and d["PRN"].size:
                tlm[(m.group(1), int(m.group(2)))] = d
    if not tlm:
        tex.missing("telemetry decoder dumps")
        return
    lnav = ephemeris_prns(run_dir / "gps_ephemeris.xml")
    cnav = ephemeris_prns(run_dir / "gps_cnav_ephemeris.xml")
    msgs = nav_message_counts(run_dir)
    fig, ax = plt.subplots(figsize=(FIG_W, 2.6))
    rows, labels, y = [], [], 0
    colors = {"1C": "C0", "2S": "C2", "L5": "C1"}
    seen = {}
    for (sig, ch), d in sorted(tlm.items(), key=lambda kv: ([b[0] for b in bands].index(kv[0][0]), kv[0][1])):
        t = d["tracking_sample_counter"] / fs
        for prn in np.unique(d["PRN"][d["PRN"] > 0]).astype(int):
            m = d["PRN"] == prn
            key = (sig, prn)
            if key not in seen:
                seen[key] = y
                labels.append(f"{BAND_LABEL[sig]} {prn}")
                y += 1
            ax.plot(t[m], np.full(m.sum(), seen[key]), "|", color=colors[sig], ms=4)
            eph = prn in (lnav if sig == "1C" else cnav)
            rows.append([BAND_LABEL[sig], prn, msgs.get(key, "--"), "yes" if eph else "no",
                         f"{t[m][0]:.1f}", f"{d['TOW_at_current_symbol_ms'][m][0]:.1f}"])
    ax.set(yticks=range(len(labels)), yticklabels=labels, xlabel="time [s]",
           title="time with a valid navigation-message time stamp")
    ax.tick_params(axis="y", labelsize=6)
    fig.tight_layout()
    tex.figure(savefig(fig, out, "telemetry", tex), "Telemetry decoder: per band and satellite, when the decoder "
               "delivered a valid transmit time (the first one comes after the first complete frame/message).")
    rows.sort(key=lambda r: (r[0], r[1]))
    tex.table(["Band", "PRN", "Messages decoded", "Ephemeris", "First valid at [s]", "TOW [s]"], rows,
              "Telemetry decoder: L1 C/A LNAV subframes and L2C/L5 CNAV messages decoded, ephemeris obtained, and "
              "the first valid time of week.")


# ------------------------------------------------------------------------------------------- 6. observables
def stage_observables(tex, out, run_dir, bands):
    tex.para("Observables.", "Pseudorange, carrier phase and Doppler of every channel at common receiver epochs.")
    path = run_dir / "observables.mat"
    if not path.exists():
        tex.missing("observables dump")
        return None
    n = sum(b[2] for b in bands)
    obs = load_observables(path, n)
    t = obs["RX_time"].max(axis=0)
    valid = obs["Flag_valid_pseudorange"] > 0
    t0 = t[t > 0].min() if np.any(t > 0) else 0
    chan_band = [band_of_channel(bands, ch) for ch in range(n)]
    fig, axes = plt.subplots(1, 2, figsize=(FIG_W, 2.8))
    per = {}
    for ch in range(n):
        for e in np.flatnonzero(valid[ch]):
            per.setdefault((chan_band[ch], int(obs["PRN"][ch, e])), {})[e] = obs["Pseudorange_m"][ch, e]
    keys = sorted(per, key=lambda k: ([b[0] for b in bands].index(k[0]), k[1]))
    colors = {"1C": "C0", "2S": "C2", "L5": "C1"}
    for i, k in enumerate(keys):
        ep = np.array(sorted(per[k]))
        axes[0].plot(t[ep] - t0, np.full(ep.size, i), "|", color=colors[k[0]], ms=3)
    axes[0].set(yticks=range(len(keys)), yticklabels=[f"{BAND_LABEL[b]} {p}" for b, p in keys],
                xlabel="time [s]", title="valid pseudoranges")
    axes[0].tick_params(axis="y", labelsize=5)
    second = bands[1][0] if len(bands) > 1 else None
    rows = []
    if second:
        for prn in sorted({p for b, p in keys if b == "1C"} & {p for b, p in keys if b == second}):
            ep = np.array(sorted(set(per[("1C", prn)]) & set(per[(second, prn)])))
            if ep.size < 2:
                continue
            d = np.array([per[(second, prn)][e] - per[("1C", prn)][e] for e in ep])
            axes[1].plot(t[ep] - t0, d, ".", ms=1, label=f"{prn}")
            drift = np.polyfit(t[ep] - t[ep][0], d, 1)[0]
            rows.append([prn, ep.size, f"{100 * ep.size / len(t):.0f}", f"{d.mean():.2f}", f"{d.std():.2f}",
                         f"{drift:.4f}"])
        axes[1].set(xlabel="time [s]", ylabel=f"P$_{{{BAND_LABEL[second]}}}$ - P$_{{L1}}$ [m]",
                    title=f"{BAND_LABEL[second]} minus L1 pseudorange")
        if rows:
            axes[1].legend(fontsize=5, ncol=3, title="PRN", title_fontsize=5, markerscale=5)
    fig.tight_layout()
    tex.figure(savefig(fig, out, "observables", tex), "Observables: epochs with a valid pseudorange per band and "
               "satellite, and the difference between the two bands' pseudoranges of the same satellite (a constant "
               "receiver inter-band bias, the same for every satellite, plus the slowly varying ionospheric delay).")
    if rows:
        tex.table(["PRN", "Common epochs", "of run [\\%]", "Mean [m]", "Std [m]", "Drift [m/s]"], rows,
                  f"Observables: P$_{{\\mathrm{{{BAND_LABEL[second]}}}}}$ $-$ P$_{{\\mathrm{{L1}}}}$ per satellite "
                  "at common epochs (similar means = shared receiver bias; std = code noise of both bands).")
    else:
        tex.missing(f"epochs with both L1 and {BAND_LABEL.get(second, 'second-band')} pseudoranges")
    return obs


# -------------------------------------------------------------------------------------------------- 7. PVT
def stats_row(label, enu, t, n_total):
    if not len(enu):
        return [label, 0, "--", "--", "--", "--", "--", "--"]
    h = np.hypot(enu[:, 0], enu[:, 1])
    m, s = enu.mean(0), enu.std(0)
    seconds = len(np.unique(np.floor(np.asarray(t, dtype=float))))  # seconds of the run with at least one fix
    return [label, len(enu), f"{min(100, 100 * seconds / n_total):.0f}" if n_total else "--",
            f"{m[0]:.1f} / {m[1]:.1f} / {m[2]:.1f}", f"{s[0]:.1f} / {s[1]:.1f} / {s[2]:.1f}",
            f"{np.sqrt(np.mean(h ** 2)):.1f}", f"{np.sqrt(np.mean(np.sum(enu ** 2, 1))):.1f}",
            f"{np.percentile(h, 95):.1f}"]


def stage_pvt(tex, out, run_dir, bands, obs):
    tex.para("PVT.", "Position from the pseudoranges and the decoded ephemeris, compared with the surveyed antenna "
             f"position ({ANTENNA_LLH[0]}$^\\circ$, {ANTENNA_LLH[1]}$^\\circ$, {ANTENNA_LLH[2]}~m).")
    sols = []
    pvt = run_dir / "pvt.mat"
    if pvt.exists():
        d = load_mat(pvt)
        lat, lon, h = d.get("latitude"), d.get("longitude"), d.get("height")
        if lat is not None:
            ok = (np.abs(lat) > 0) & np.isfinite(lat)
            from gnss_spp import enu_matrix, llh_to_ecef
            ref, r = llh_to_ecef(*ANTENNA_LLH), enu_matrix(ANTENNA_LLH[0], ANTENNA_LLH[1])
            enu = np.array([r @ (llh_to_ecef(a, b, c) - ref) for a, b, c in zip(lat[ok], lon[ok], h[ok])]).reshape(-1, 3)
            sols.append(("gnss-sdr, L1 C/A", enu, d.get("RX_time", np.arange(ok.size))[ok] if ok.any() else []))
    spp_epochs = {}
    eph_path = run_dir / "gps_ephemeris.xml"
    if obs is not None and eph_path.exists():
        eph = load_lnav_ephemeris(eph_path)
        chan_bands = [band_of_channel(bands, ch) for ch in range(sum(b[2] for b in bands))]
        second = bands[1][0] if len(bands) > 1 else None
        if second:
            ep = solve(obs, chan_bands, eph, second_band=second)
            spp_epochs[second] = ep
            sols.append((f"iono-free L1 C/A + {BAND_LABEL[second]}", enu_errors(ep, ANTENNA_LLH),
                         np.array([e["t"] for e in ep])))
    if not sols:
        tex.missing("PVT solutions")
        return
    rx = obs["RX_time"].max(axis=0) if obs is not None else np.zeros(0)
    t0 = rx[rx > 0].min() if np.any(rx > 0) else min((s[2][0] for s in sols if len(s[2])), default=0)
    n_total = int(np.ptp(rx[rx > 0])) if np.any(rx > 0) else 0
    fig = plt.figure(figsize=(FIG_W, 1.0 + 1.3 * len(sols)))
    gs = fig.add_gridspec(len(sols), 2, width_ratios=[1, 1.7])
    ax_h = fig.add_subplot(gs[:, 0])
    lims = []
    for i, (label, enu, t) in enumerate(sols):
        ax = fig.add_subplot(gs[i, 1])
        if len(enu):
            ax_h.plot(enu[:, 0], enu[:, 1], ".", ms=1.5, alpha=0.5, color=f"C{i}", label=label)
            lims.append(np.percentile(np.abs(enu[:, :2]), 95))
            tt = np.asarray(t, dtype=float) - t0
            for k, comp in enumerate("ENU"):
                ax.plot(tt, enu[:, k], lw=0.6, color=["C0", "C1", "C2"][k], label=comp)
        ax.set(ylabel="error [m]", title=label, xlim=(0, n_total or None))
        ax.title.set_fontsize(7)
        ax.legend(fontsize=5, ncol=3, loc="upper right")
        if i == len(sols) - 1:
            ax.set_xlabel("time [s]")
    ax_h.plot(0, 0, "k+", ms=8, mew=1.5, label="surveyed antenna")
    lim = max(5, 1.2 * max(lims, default=5))
    ax_h.set(xlabel="east [m]", ylabel="north [m]", xlim=(-lim, lim), ylim=(-lim, lim), aspect="equal",
             title="horizontal position error")
    ax_h.legend(fontsize=5)
    fig.tight_layout()
    tex.figure(savefig(fig, out, "pvt", tex), "PVT: position error with respect to the surveyed antenna position. "
               "L1 C/A: gnss-sdr's own solution (broadcast ionosphere model). Iono-free: computed from gnss-sdr's "
               "observables and decoded ephemeris (gnss\\_spp.py), removing the ionospheric delay with the second band "
               "(pseudoranges carrier-smoothed over 100~s).")
    sky = next((ep for ep in spp_epochs.values() if ep), None)
    if sky:
        fig = plt.figure(figsize=(FIG_W / 2.2, FIG_W / 2.2))
        ax = fig.add_subplot(projection="polar")
        ax.set_theta_zero_location("N")
        ax.set_theta_direction(-1)
        tracks = {}
        for e in sky:
            for prn, el, az in zip(e["prns"], e["elev"], e["az"]):
                tracks.setdefault(prn, []).append((az, 90 - np.degrees(el)))
        for prn, pts in sorted(tracks.items()):
            a = np.array(pts)
            ax.plot(a[:, 0], a[:, 1], lw=2)
            ax.annotate(str(prn), (a[-1, 0], a[-1, 1]), fontsize=6)
        ax.set_rlim(0, 90)
        ax.set_rticks([0, 30, 60, 90])
        ax.set_yticklabels(["90°", "60°", "30°", "0°"], fontsize=5)
        tex.figure(savefig(fig, out, "skyplot", tex), "PVT: sky plot of the satellites in the dual-frequency "
                   "solution (azimuth, elevation) during the recording.", width=0.45)
    tex.table_t(["Solution", "Solutions", "Run with fix [\\%]", "Mean E / N / U [m]", "Std E / N / U [m]", "RMS 2-D [m]",
               "RMS 3-D [m]", "95\\% horiz. [m]"], [stats_row(l, e, t, n_total) for l, e, t in sols],
              "PVT: position error statistics with respect to the surveyed antenna (mean = accuracy, std = "
              "precision; \\% of the run's seconds with a position; gnss-sdr outputs several solutions per second, "
              "the iono-free one is computed once per second).")


# ------------------------------------------------------------------------------------------------------- main
def main():
    args = parse_args()
    run_dir, archive = args.run_dir, args.recording
    name = archive.name.removesuffix(".sigmf")
    out = args.out or args.tex_root / "gnss_sdr" / name
    out.mkdir(parents=True, exist_ok=True)
    tex = Tex(args.tex_root, name)
    metas = read_archive_meta(archive)
    try:
        conf_name, values = conf_values(run_dir)
    except StopIteration:
        tex.missing("GNSS-SDR run (no configuration in its output directory)")
        tex.write(out / "results.tex")
        return
    bands = bands_of(values)
    # the recording's metadata table in main.tex shows it (report_lib.session_subsection_tex reads this file)
    (out / CONF_NAME_FILE).write_text(conf_name + "\n")
    fs = float(values.get("GNSS-SDR.internal_fs_sps", 0))
    stage_source(tex, out, archive, metas, bands)
    stage_conditioner(tex, values, metas, bands)
    stage_acquisition(tex, out, run_dir, bands, fs)
    stage_tracking(tex, out, run_dir, bands, fs)
    stage_telemetry(tex, out, run_dir, bands, fs)
    obs = stage_observables(tex, out, run_dir, bands)
    stage_pvt(tex, out, run_dir, bands, obs)
    tex.write(out / "results.tex")
    print(f"wrote {out / 'results.tex'} and {len(list(out.glob('*.pdf')))} figures")


if __name__ == "__main__":
    main()
