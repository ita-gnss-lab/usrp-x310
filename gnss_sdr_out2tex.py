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

A second run of the same recording with another configuration (e.g. Galileo E1 + E5a next to the GPS L1/L5 run) is
written with --tag: its figures are prefixed <tag>_, its block goes to results_<tag>.tex, every stage heading gets
--title appended, and the signal-source stage (the same raw recording as the first run) is skipped.

Examples:
  uv run gnss_sdr_out2tex.py gnss_sdr_output/x310_20261021T193000Z --recording recordings/x310_20261021T193000Z.sigmf
  uv run gnss_sdr_out2tex.py gnss_sdr_output/x310_20261022T190000Z_galileo --recording \\
      recordings/x310_20261022T190000Z.sigmf --tag galileo --title "Galileo E1 + E5a"
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
# gnss-sdr signal codes, in its channel order
SIGNALS = ("1C", "2S", "L5", "1B", "5X")
BAND_LABEL = {"1C": "GPS L1 C/A", "2S": "GPS L2C", "L5": "GPS L5", "1B": "Galileo E1", "5X": "Galileo E5a"}
BAND_FREQ_MHZ = {"1C": 1575.42, "2S": 1227.60, "L5": 1176.45, "1B": 1575.42, "5X": 1176.45}
CHIP_RATE = {"1C": 1.023e6, "2S": 1.023e6, "L5": 10.23e6, "1B": 1.023e6, "5X": 10.23e6}
BAND_COLOR = {"1C": "C0", "2S": "C2", "L5": "C1", "1B": "C3", "5X": "C4"}
# dual-frequency pairs (first band, second band) whose pseudorange difference the observables stage shows
BAND_PAIRS = (("1C", "L5"), ("1C", "2S"), ("1B", "5X"))
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
    p.add_argument("--tag", default="", help="second run of the same recording: prefix of its figures and "
                   "results_<tag>.tex; the signal-source stage is skipped (default: none, the recording's first run)")
    p.add_argument("--title", default="", help="appended to every stage heading of this run, e.g. 'Galileo E1 + E5a'")
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
    """[(signal code, first channel, n channels)] in gnss-sdr's channel order (SIGNALS)."""
    out, first = [], 0
    for sig in SIGNALS:
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
# (gnss_sdr_conf_<tag>.txt for a --tag run)
FIG_DPI = 200  # raster figures: vector PDFs of plots with ~10^4-10^5 points made the report too heavy to render


def fig_path(out, name, tag=""):
    return out / (f"{tag}_{name}.png" if tag else f"{name}.png")


def savefig(fig, out, name, tex):
    path = fig_path(out, name, tex.tag)
    fig.savefig(path, dpi=FIG_DPI)
    plt.close(fig)
    return path.relative_to(tex.root).as_posix()


class Tex:
    """Collects results.tex: one subsubsection per stage, with its figures and tables."""

    def __init__(self, root, name, tag="", title=""):
        self.root, self.name, self.tag, self.title, self.lines = root, name, tag, title, []

    def para(self, title, text=""):
        """One receiver stage: a \\subsubsection under the recording's \\subsection (title without its final dot,
        plus this run's --title, e.g. 'Acquisition (Galileo E1 + E5a)')."""
        heading = title.rstrip(".") + (f" ({self.title})" if self.title else "")
        self.lines += [f"\\subsubsection{{{heading}}}", text, ""]

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

    def table_t(self, header, rows, caption, span_rows=(), col_width="4.2cm"):
        """table() transposed: one row per quantity (header[1:]), one column per item (rows' first cells), for
        tables with few items but many quantities, which would be too wide the other way round. Each item column is
        col_width wide, centred, and wraps longer text onto more lines."""
        new_header = [header[0]] + [str(r[0]) for r in rows]
        new_rows = [[header[i]] + [r[i] for r in rows] for i in range(1, len(header))]
        self.table(new_header, new_rows, caption, span_rows=span_rows,
                   align="l" + f">{{\\centering\\arraybackslash}}p{{{col_width}}}" * len(rows))

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
    tex.para("Signal source.", "Raw I/Q samples of each channel analysed within the window "
             f"[{SOURCE_OFFSET_S:g}, {SOURCE_OFFSET_S + SOURCE_SECONDS:g}]~s after the start recording instant.")
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
        # The samples are integers, so their distribution is a probability mass function (PMF): the fraction of
        # samples taking each integer value. Shown over the whole integer range, so the clipping limits (red) show
        # how much headroom the noise leaves
        lim = 1.08 * full
        for comp, label, style in ((x.real, "I", "bar"), (x.imag, "Q", "step")):
            k, n = np.unique(comp.astype(np.int64), return_counts=True)
            pmf = n / comp.size
            if style == "bar":
                ax.bar(k, pmf, width=1.0, alpha=0.6, color="C0", label=label)
            else:  # Q as a dashed outline: it overlaps I almost exactly, a second filled one would hide it
                ax.step(k, pmf, where="mid", ls="--", lw=1.0, color="C1", label=label)
        # Gaussian reference, as a PMF: its density at each integer (bins of width 1)
        k = np.arange(-int(min(full, 6 * sd)), int(min(full, 6 * sd)) + 1)
        ax.plot(k, np.exp(-k ** 2 / (2 * sd ** 2)) / (sd * np.sqrt(2 * np.pi)), "k", lw=0.8, label="Gaussian")
        for edge in (-full, full):
            ax.axvline(edge, color="red", lw=0.8, label=f"clipping ($\\pm${full})" if edge > 0 else None)
        if g["core:datatype"] != "ci8":
            # 16-bit: the noise (std of tens) is tiny next to +-32767, so a symmetric-log x axis (linear within
            # +-5 std, logarithmic beyond) shows both its shape and the limits
            ax.set_xscale("symlog", linthresh=5 * sd)
        ax.set(xlabel="sample value", ylabel="probability", title=f"ch{ch} I/Q probability mass function",
               xlim=(-lim, lim))
        handles, labels = ax.get_legend_handles_labels()
        order = sorted(range(len(labels)), key=lambda j: ["I", "Q", "Gaussian"].index(labels[j])
                       if labels[j] in ("I", "Q", "Gaussian") else 3)  # bars are listed last by default
        ax.legend([handles[j] for j in order], [labels[j] for j in order], fontsize=6)
        clip = np.mean(np.abs(v) >= full) * 100
        bytes_per = {"ci8": 2, "ci16_le": 4}[g["core:datatype"]]
        rows.append([f"ch{ch}", f"{freq:.2f}", f"{fs / 1e6:g}", tex_escape(g["core:datatype"]),
                     f"{bytes_per} ({bytes_per // 2} for I, {bytes_per // 2} for Q)", f"$\\pm${full}",
                     f"{g['x310:gain_db']:g}", f"{x.real.std():.1f} / {x.imag.std():.1f}",
                     f"{np.mean(x.real):.2f} / {np.mean(x.imag):.2f}",
                     f"{x.real.min():.0f} / {x.real.max():.0f}", f"{x.imag.min():.0f} / {x.imag.max():.0f}",
                     f"{full / sd:.1f}", f"{clip:.3f}", f"{g.get('x310:lost_samples', 0):,}"])
    fig.tight_layout()
    rel = savefig(fig, out, "source", tex)
    tex.figure(rel, "Signal source: power spectral density (the receiver passband over a flat thermal-noise floor; "
               "GNSS signals are below the noise, so only interference would stand out) and probability mass "
               "functions of the integer I/Q sample values (Gaussian noise, centred, not clipped).")
    tex.table_t(["Channel", "Frequency [MHz]", "Rate [MS/s]", "Format", "Bytes per complex sample", "Full scale",
                 "Gain [dB]", "Std I / Q", "Mean I / Q", "Min / max I", "Min / max Q", "Headroom (full scale / std)",
                 "Clipped [\\%]", "Lost samples"], rows,
                "Signal source: sample statistics per channel (headroom: how many standard deviations of the noise "
                "fit before clipping).")


# ------------------------------------------------------------------------------------ 2. signal conditioner
def stage_conditioner(tex, values, metas, bands):
    tex.para("Signal conditioner.")
    internal = float(values.get("GNSS-SDR.internal_fs_sps", 0))
    rows = []
    for i in sorted(metas):
        # every band read from this file, e.g. GPS L1 C/A and Galileo E1 from the L1 one
        file_bands = [sig for sig, first, _ in bands if int(values.get(f"Channel{first}.RF_channel_ID", 0)) == i]
        g = metas[i]["global"]
        fs = g["core:sample_rate"]
        item = values.get("SignalSource.item_type", values.get(f"SignalSource{i}.item_type", "?"))
        k = round(internal / fs) if fs else 0
        resamp = values.get(f"Resampler{i}.implementation", "?")
        up = (f"$\\times${k} zero-order hold, offline (\\texttt{{upsample\\_iq.py}})"
              if k > 1 and resamp == "Pass_Through" else resamp)
        rows.append([f"ch{i} ({', '.join(BAND_LABEL[sig] for sig in file_bands)})", tex_escape(g["core:datatype"]), tex_escape(item),
                     tex_escape(values.get(f"DataTypeAdapter{i}.implementation", "?")),
                     tex_escape(values.get(f"InputFilter{i}.implementation", "?")),
                     up if k > 1 and resamp == "Pass_Through" else tex_escape(up),  # the x5 note is LaTeX already
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
        # the correlation peak: circled, with a red arrow to its code delay on the right
        peak_x, peak_y = chips[i_del], norm[i_dop, i_del]
        axes[1].plot(peak_x, peak_y, "o", ms=9, mfc="none", mec="red", mew=1.2)
        # label always to the right of the peak; near the right edge it is right-aligned to the axes and lower, so
        # the arrow still points right (down to the label's start)
        frac = peak_x / chips[-1]
        room = frac < 0.6
        axes[1].annotate(f"{peak_x:.1f} chips", xy=(peak_x, peak_y), xycoords="data",
                         xytext=(frac + 0.1, 0.72) if room else (0.99, 0.42), textcoords="axes fraction",
                         ha="left" if room else "right", color="red", fontsize=8, va="center",
                         arrowprops=dict(arrowstyle="<-", color="red", lw=1.0, shrinkA=2, shrinkB=6))
        axes[1].set(xlabel="code delay [chips]", ylabel="power / noise floor",
                    title=f"whole code period at {dopp[i_dop]:.0f} Hz", ylim=(0, 1.1 * peak_y))
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
              "(or do not broadcast that signal, e.g. older GPS satellites on L5/L2C) are rejected below the threshold.")


# ---------------------------------------------------------------------------------------------- 4. tracking
def losses_of_lock(run_dir):
    counts = {}
    info = run_dir / "gnss-sdr.INFO"
    if info.exists():
        for m in re.finditer(r"Loss of lock in channel (\d+)", info.read_text(errors="replace")):
            counts[int(m.group(1))] = counts.get(int(m.group(1)), 0) + 1
    return counts


def stage_tracking(tex, out, run_dir, bands, fs):
    tex.para("Tracking.")
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
                # time tracked: the sum of the gaps between consecutive outputs (1 ms during pull-in, up to 20 ms
                # once bit-synchronized: count x spacing would overcount), without the gaps where the channel was
                # off this satellite
                gaps = np.diff(t[m])
                rows.append([BAND_LABEL[sig], ch, prn, f"{cn0:.1f}", f"{locked:.0f}", losses.get(ch, 0),
                             f"{gaps[gaps < 1].sum():.0f}" if m.sum() > 1 else "--"])
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
               "the data component on the Q axis), or a single one for a dataless pilot (Galileo E1-C and E5a-Q, "
               "tracked here), mean the PLL is phase-locked; a ring would mean it is not. \\texttt{Prompt\\_I}/\\texttt{Prompt\\_Q} are 32-bit floats (sums of the samples over one integration period, in file units): they never clip, only overflowing beyond $\\pm 3.4\\times10^{38}$, far above the $N\\cdot\\text{full scale}\\cdot\\sqrt{2}$ a sum of $N$ samples can reach.")
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
    # e.g. "New GPS NAV message received in channel 4: subframe 3 from satellite GPS PRN 32 (Block IIF) ...",
    # "New Galileo E1 I/NAV message received in channel 6: ephemeris from satellite Galileo PRN E14 (...) ...",
    # "New Galileo E5a F/NAV message ..."
    message_band = {"GPS NAV": "1C", "GPS L5 CNAV": "L5", "GPS CNAV": "2S", "Galileo E1 I/NAV": "1B",
                    "Galileo E1 I/NAV reduced CED": "1B", "Galileo E5a F/NAV": "5X"}
    for m in re.finditer(r"New ((?:GPS|Galileo) [^\n]*?) message received in channel \d+: [^\n]*?satellite "
                         r"(?:GPS|Galileo) PRN E?(\d+)", text):
        band = message_band.get(m.group(1))
        if band:
            counts[(band, int(m.group(2)))] = counts.get((band, int(m.group(2))), 0) + 1
    return counts


def stage_telemetry(tex, out, run_dir, bands, fs):
    tex.para("Telemetry decoder.")
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
    galileo = ephemeris_prns(run_dir / "gal_ephemeris.xml")
    ephemeris_of = {"1C": lnav, "2S": cnav, "L5": cnav, "1B": galileo, "5X": galileo}
    msgs = nav_message_counts(run_dir)
    fig, ax = plt.subplots(figsize=(FIG_W, 2.6))
    rows, labels, y = [], [], 0
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
            ax.plot(t[m], np.full(m.sum(), seen[key]), "|", color=BAND_COLOR[sig], ms=4)
            eph = prn in ephemeris_of[sig]
            rows.append([BAND_LABEL[sig], prn, msgs.get(key, "--"), "yes" if eph else "no",
                         f"{t[m][0]:.1f}", f"{d['TOW_at_current_symbol_ms'][m][0]:.1f}"])
    ax.set(yticks=range(len(labels)), yticklabels=labels, xlabel="time [s]",
           title="time with a valid navigation-message time stamp")
    ax.tick_params(axis="y", labelsize=6)
    fig.tight_layout()
    tex.figure(savefig(fig, out, "telemetry", tex), "Telemetry decoder: per band and satellite, when the decoder "
               "delivered a valid transmit time (the first one comes after the first complete frame/message).")
    rows.sort(key=lambda r: (r[0], r[1]))
    message_kind = {"1C": "GPS L1 C/A LNAV subframes", "2S": "GPS L2C CNAV messages", "L5": "GPS L5 CNAV messages",
                    "1B": "Galileo E1 I/NAV words", "5X": "Galileo E5a F/NAV pages"}
    tex.table(["Band", "PRN", "Messages decoded", "Ephemeris", "First valid at [s]", "TOW [s]"], rows,
              "Telemetry decoder: " + ", ".join(message_kind[b[0]] for b in bands) + " decoded, ephemeris "
              "obtained, and the first valid time of week.")


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
    for i, k in enumerate(keys):
        ep = np.array(sorted(per[k]))
        axes[0].plot(t[ep] - t0, np.full(ep.size, i), "|", color=BAND_COLOR[k[0]], ms=3)
    axes[0].set(yticks=range(len(keys)), yticklabels=[f"{BAND_LABEL[b]} {p}" for b, p in keys],
                xlabel="time [s]", title="valid pseudoranges")
    axes[0].tick_params(axis="y", labelsize=5)
    # the run's dual-frequency pairs, e.g. GPS L1 C/A -> L5, or Galileo E1 -> E5a
    run_signals = [b[0] for b in bands]
    pairs = [(first, second) for first, second in BAND_PAIRS if first in run_signals and second in run_signals]
    rows = []
    for first, second in pairs:
        for prn in sorted({p for b, p in keys if b == first} & {p for b, p in keys if b == second}):
            ep = np.array(sorted(set(per[(first, prn)]) & set(per[(second, prn)])))
            if ep.size < 2:
                continue
            d = np.array([per[(second, prn)][e] - per[(first, prn)][e] for e in ep])
            prefix = "E" if first == "1B" else ""  # Galileo PRNs as E11, beside GPS PRN numbers
            axes[1].plot(t[ep] - t0, d, ".", ms=1, label=f"{prefix}{prn}")
            drift = np.polyfit(t[ep] - t[ep][0], d, 1)[0]
            rows.append([f"{BAND_LABEL[second]} $-$ {BAND_LABEL[first]}", prn, ep.size,
                         f"{100 * ep.size / len(t):.0f}", f"{d.mean():.2f}", f"{d.std():.2f}", f"{drift:.4f}"])
    pair_names = " / ".join(f"{BAND_LABEL[second]} $-$ {BAND_LABEL[first]}" for first, second in pairs)
    if pairs:
        axes[1].set(xlabel="time [s]", ylabel="pseudorange difference [m]", title=pair_names)
        axes[1].title.set_fontsize(7)
        if rows:
            axes[1].legend(fontsize=5, ncol=3, title="PRN", title_fontsize=5, markerscale=5)
    fig.tight_layout()
    tex.figure(savefig(fig, out, "observables", tex), "Observables: epochs with a valid pseudorange per band and "
               "satellite, and the difference between the two bands' pseudoranges of the same satellite (a constant "
               "receiver inter-band bias, the same for every satellite, plus the slowly varying ionospheric delay).")
    if rows:
        tex.table(["Difference", "PRN", "Common epochs", "of run [\\%]", "Mean [m]", "Std [m]", "Drift [m/s]"], rows,
                  "Observables: pseudorange difference between the two bands of each satellite at common epochs "
                  "(similar means = shared receiver bias; std = code noise of both bands).")
    elif pairs:
        tex.missing(f"epochs with pseudoranges on both bands ({pair_names})")
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


def gnss_sdr_pvt_label(values, bands):
    """Which signals gnss-sdr's own (RTKLIB) solution uses: with Iono-Free-LC every band, otherwise only the L1/E1
    signals (single frequency, broadcast ionosphere model); GPS + Galileo are solved together."""
    run_signals = [b[0] for b in bands]
    is_iono_free = values.get("PVT.iono_model", "") == "Iono-Free-LC"
    used = run_signals if is_iono_free else [s for s in run_signals if s in ("1C", "1B")]
    return "gnss-sdr, " + " + ".join(BAND_LABEL[s] for s in used) + (", iono-free" if is_iono_free else "")


def stage_pvt(tex, out, run_dir, bands, obs, values):
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
            sols.append((gnss_sdr_pvt_label(values, bands), enu, d.get("RX_time", np.arange(ok.size))[ok] if ok.any() else []))
    spp_epochs = {}
    eph_path = run_dir / "gps_ephemeris.xml"
    run_signals = [b[0] for b in bands]
    # gnss_spp.py is GPS-only: a GPS L1 C/A + L5 (or L2C) run gets its iono-free solution too
    second = next((s for s in ("L5", "2S") if s in run_signals), None) if "1C" in run_signals else None
    if obs is not None and eph_path.exists() and second:
        eph = load_lnav_ephemeris(eph_path)
        chan_bands = [band_of_channel(bands, ch) for ch in range(sum(b[2] for b in bands))]
        ep = solve(obs, chan_bands, eph, second_band=second)
        spp_epochs[second] = ep
        sols.append((f"gnss\\_spp.py, GPS L1 C/A + {BAND_LABEL[second].removeprefix('GPS ')}, iono-free", enu_errors(ep, ANTENNA_LLH),
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
    caption = ("PVT: position error with respect to the surveyed antenna position. gnss-sdr: GNSS-SDR's own RTKLIB "
               "solution" + ("" if values.get("PVT.iono_model") == "Iono-Free-LC" else
                             " (single frequency, broadcast ionosphere model)") + ".")
    if spp_epochs:
        caption += (" gnss\\_spp.py: computed from gnss-sdr's observables and decoded GPS ephemeris, removing the "
                    "ionospheric delay with the second band (pseudoranges carrier-smoothed over 100~s).")
    tex.figure(savefig(fig, out, "pvt", tex), caption)
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
            # the track over the recording (a few degrees in 5 min) and a large dot at the satellite's last position
            line, = ax.plot(a[:, 0], a[:, 1], lw=3)
            ax.plot(a[-1, 0], a[-1, 1], "o", ms=11, color=line.get_color(), mec="black", mew=0.6)
            ax.annotate(str(prn), (a[-1, 0], a[-1, 1]), xytext=(7, 4), textcoords="offset points",
                        fontsize=9, fontweight="bold")
        ax.set_rlim(0, 90)
        ax.set_rticks([0, 30, 60, 90])
        ax.set_yticklabels(["90°", "60°", "30°", "0°"], fontsize=5)
        tex.figure(savefig(fig, out, "skyplot", tex), "PVT: sky plot of the GPS satellites in the gnss\\_spp.py dual-frequency "
                   "solution (azimuth, elevation) during the recording.", width=0.45)
    tex.table_t(["Solution", "Solutions", "Run with fix [\\%]", "Mean E / N / U [m]", "Std E / N / U [m]", "RMS 2-D [m]",
               "RMS 3-D [m]", "95\\% horiz. [m]"], [stats_row(l, e, t, n_total) for l, e, t in sols],
              "PVT: position error statistics with respect to the surveyed antenna (mean = accuracy, std = "
              "precision; \\% of the run's seconds with a position; gnss-sdr outputs several solutions per second, "
              "gnss\\_spp.py computes one per second).")


# ------------------------------------------------------------------------------------------------------- main
def main():
    args = parse_args()
    run_dir, archive = args.run_dir, args.recording
    name = archive.name.removesuffix(".sigmf")
    out = args.out or args.tex_root / "gnss_sdr" / name
    out.mkdir(parents=True, exist_ok=True)
    tex = Tex(args.tex_root, name, args.tag, args.title)
    results = out / (f"results_{args.tag}.tex" if args.tag else "results.tex")
    metas = read_archive_meta(archive)
    try:
        conf_name, values = conf_values(run_dir)
    except StopIteration:
        tex.missing("GNSS-SDR run (no configuration in its output directory)")
        tex.write(results)
        return
    bands = bands_of(values)
    # the recording's metadata table in main.tex shows it (report_lib.session_subsection_tex reads this file)
    conf_name_file = CONF_NAME_FILE.replace(".txt", f"_{args.tag}.txt") if args.tag else CONF_NAME_FILE
    (out / conf_name_file).write_text(conf_name + "\n")
    fs = float(values.get("GNSS-SDR.internal_fs_sps", 0))
    if not args.tag:  # a --tag run reads the same raw recording as the first run, already shown there
        stage_source(tex, out, archive, metas, bands)
    stage_conditioner(tex, values, metas, bands)
    stage_acquisition(tex, out, run_dir, bands, fs)
    stage_tracking(tex, out, run_dir, bands, fs)
    stage_telemetry(tex, out, run_dir, bands, fs)
    obs = stage_observables(tex, out, run_dir, bands)
    stage_pvt(tex, out, run_dir, bands, obs, values)
    tex.write(results)
    print(f"wrote {results} and {len(list(out.glob(f'{args.tag}_*.png' if args.tag else '*.png')))} figures")


if __name__ == "__main__":
    main()
