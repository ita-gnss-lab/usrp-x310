"""LaTeX side of the report (report/main.tex): read the recordings' metadata, append each recording to the
"Temporary GNSS data" part (the last 15 recording days), refresh the temporary and permanent recording calendars, and
compile the PDF.

Used by update_report.py (after every recording, from systemd/x310-record.sh), by gnss_sdr_out2tex.py
(tex_escape) and by send_whatsapp_msg.py (load_sessions, compile_report_pdf for the 15-day WhatsApp message).
"""

import glob
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ORDINALS = ["First", "Second", "Third", "Fourth", "Fifth"]  # per-day record count never exceeds 2 in practice
# the "Temporary GNSS data" part keeps only this many most recent recording days (days with at least one recording), so
# the PDF stays under WhatsApp's ~18 MB document limit (~1 MB per day); older days stay in git
KEPT_RECORDING_DAYS = 15
# the "Temporary GNSS data" part (day sections, appended and pruned here) ends where the permanent-data part starts
PERMANENT_PART_MARKER = r"\part{Permanent scintillating data}"
TEMPORARY_CALENDAR_CAPTION = "Temporary recording calendar."
TEMPORARY_CALENDAR_LABEL = "tab:temporary-calendar"
PERMANENT_CALENDAR_CAPTION = "Permanent recording calendar."
PERMANENT_CALENDAR_LABEL = "tab:permanent-calendar"
BAND_NAMES = {1575.42: "L1", 1227.60: "L2", 1176.45: "L5"}  # GPS carrier [MHz] -> band name


def freq_cell(freq_hz):
    """'1575.420 MHz (L1)': the centre frequency, plus the GPS band name when it is one."""
    mhz = freq_hz / 1e6
    band = BAND_NAMES.get(round(mhz, 2))
    return f"{mhz:.3f} MHz" + (f" ({band})" if band else "")


def _channel_num(meta_name):
    """'<name>_ch<k>' -> k, from a SigMF Recording's base name (no extension)."""
    m = re.search(r"_ch(\d+)$", meta_name)
    return int(m.group(1)) if m else 0


def load_sessions(out_dir):
    """Read every <name>.sigmf archive in out_dir (one SigMF Archive per recording session,
    bundling a Collection + each channel's Recording) directly from the tar -- no extraction
    needed, including for the (potentially huge) .sigmf-data members, whose size comes
    straight from the tar header -- into one entry per session, channels sorted."""
    sessions = []
    for archive_path in sorted(out_dir.glob("*.sigmf")):
        try:
            with tarfile.open(archive_path) as tar:
                tar.getmembers()
        except (tarfile.TarError, OSError) as e:  # e.g. truncated by a full disk while it was being bundled
            print(f"warning: skipping unreadable archive {archive_path}: {e}")
            continue
        with tarfile.open(archive_path) as tar:
            data_sizes = {m.name: m.size for m in tar.getmembers() if m.name.endswith(".sigmf-data")}
            channels = []
            for member in tar.getmembers():
                if not member.name.endswith(".sigmf-meta"):
                    continue
                meta = json.loads(tar.extractfile(member).read())
                base = Path(member.name).name.removesuffix(".sigmf-meta")
                size = data_sizes.get(f"{Path(member.name).parent}/{base}.sigmf-data", 0)
                channels.append((_channel_num(base), meta, size))
        channels.sort()
        dt = datetime.fromisoformat(channels[0][1]["captures"][0]["core:datetime"].replace("Z", "+00:00"))
        sessions.append({
            "name": archive_path.stem,
            "datetime": dt,
            "channels": channels,
            "dropped": sum(meta["global"]["x310:lost_samples"] for _, meta, _ in channels),
            "bytes": sum(size for _, _, size in channels),
        })
    return sorted(sessions, key=lambda s: s["datetime"])


def ordinal(n):
    """1 -> 'First', 2 -> 'Second', ... falls back to '6th record' etc. past ORDINALS."""
    if 1 <= n <= len(ORDINALS):
        return ORDINALS[n - 1]
    return f"{n}th"


_TEX_SPECIAL = str.maketrans({
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
})


def tex_escape(text):
    """Escape LaTeX special characters in a value pulled from recording metadata."""
    return str(text).translate(_TEX_SPECIAL)


def session_subsection_tex(session, record_number, tex_dir=Path("report")):
    """Build one '\\subsection{...}' + metadata table for a single recording session, followed by its GNSS-SDR
    results (\\input of gnss_sdr/<name>/results.tex, written by gnss_sdr_out2tex.py after the recording was
    processed, then of any results_<tag>.tex of further runs, e.g. results_galileo.tex) when tex_dir has them."""
    g0 = session["channels"][0][1]["global"]
    cap0 = session["channels"][0][1]["captures"][0]
    start_str = session["datetime"].strftime("%Y-%m-%d %H:%M:%S UTC")
    sample_type = tex_escape(g0["core:sample_type_desc"])

    channel_cols = " & ".join(f"\\textbf{{Channel {ch}}}" for ch, _, _ in session["channels"])
    freq_row = " & ".join(freq_cell(meta["captures"][0]["core:frequency"]) for _, meta, _ in session["channels"])
    gain_row = " & ".join(f"{meta['global']['x310:gain_db']:.1f} dB" for _, meta, _ in session["channels"])
    antenna_row = " & ".join(tex_escape(meta["global"]["x310:antenna"]) for _, meta, _ in session["channels"])
    daughterboard_row = " & ".join(tex_escape(meta["global"]["x310:daughterboard"])
                                    for _, meta, _ in session["channels"])
    lost_samp_row = " & ".join(str(meta["global"]["x310:lost_samples"]) for _, meta, _ in session["channels"])
    size_row = " & ".join(f"{size / 1e6:.1f} MB" for _, _, size in session["channels"])
    written_row = " & ".join(f"{meta['global']['x310:written_samples']:,}" for _, meta, _ in session["channels"])
    # each channel has its own sample rate (e.g. L1 at 4 MS/s, L5 at 20 MS/s), so rate and
    # duration are per channel too -- duration from each channel's own file size and rate
    rate_row = " & ".join(f"{meta['global']['core:sample_rate'] / 1e6:.3f} MS/s" for _, meta, _ in session["channels"])
    duration_row = " & ".join(
        f"{size / meta['global']['core:bytes_per_sample'] / meta['global']['core:sample_rate']:.1f} s"
        if meta["global"]["core:sample_rate"] else "0.0 s"
        for _, meta, size in session["channels"])

    n_channels = len(session["channels"])
    # centered across the channel columns, wrapped onto more lines beyond 7 cm (e.g. the long sample-type text)
    span = f"\\multicolumn{{{n_channels}}}{{>{{\\centering\\arraybackslash}}p{{7cm}}|}}"
    col_spec = "|l|" + "l|" * n_channels  # vertical lines between every column

    rows = [
        f"Start time & {span}{{{start_str}}} \\\\",
        f"Frequency & {freq_row} \\\\",
        f"Duration & {duration_row} \\\\",
        f"Sample rate & {rate_row} \\\\",
        f"Sample type & {span}{{{sample_type}}} \\\\",
        f"Gain & {gain_row} \\\\",
        f"Antenna & {antenna_row} \\\\",
        f"File size & {size_row} \\\\",
        f"Samples written & {written_row} \\\\",
        f"Lost samples & {lost_samp_row} \\\\",
        f"Clock source & {span}{{{tex_escape(g0['x310:clock_source'])}}} \\\\",
        f"Time source & {span}{{{tex_escape(g0['x310:time_source'])}}} \\\\",
        f"Motherboard & {span}{{{tex_escape(g0['core:motherboard'])}}} \\\\",
        f"Daughterboard & {daughterboard_row} \\\\",
        f"Recorder & {span}{{{tex_escape(g0['core:recorder'])}}} \\\\",
    ]
    # written by gnss_sdr_out2tex.py: gnss_sdr_conf.txt for the first run, gnss_sdr_conf_<tag>.txt for further runs
    conf_files = sorted((tex_dir / "gnss_sdr" / session["name"]).glob("gnss_sdr_conf*.txt"))
    if conf_files:
        confs = ", ".join(f"\\texttt{{{tex_escape(f.read_text().strip())}}}" for f in conf_files)
        rows.append(f"GNSS-SDR configuration & {span}{{{confs}}} \\\\")

    lines = [
        f"\\subsection{{{ordinal(record_number)} record}}",
        r"\begin{table}[H]",
        r"  \centering",
        f"  \\begin{{tabular}}{{{col_spec}}}",
        r"    \hline",
        f"    \\textbf{{Parameter}} & {channel_cols} \\\\",
        r"    \hline",
    ]
    for row in rows:
        lines.append(f"    {row}")
        lines.append(r"    \hline")  # full grid: a line after every row, not just header/end
    lines += [
        r"  \end{tabular}",
        f"  \\caption{{Metadata for recording \\texttt{{{tex_escape(session['name'])}}}, "
        f"captured starting {cap0['core:datetime']}.}}",
        r"\end{table}",
    ]
    results_dir = tex_dir / "gnss_sdr" / session["name"]
    # the first run's results.tex sorts before results_<tag>.tex
    results = sorted(results_dir.glob("results*.tex"), key=lambda f: (f.name != "results.tex", f.name))
    if results:
        lines += [""] + [f"\\input{{{f.relative_to(tex_dir).as_posix()}}}" for f in results]
    else:
        lines += ["", r"\textit{GNSS-SDR processing of this recording is not available.}"]
    return "\n".join(lines)


# with -file-line-error, errors are logged as "./main.tex:185: <message>" instead of "! <message>"
_LATEX_ERROR_RE = re.compile(r"^! |^\S+\.tex:\d+: |LaTeX Error:|Emergency stop|Fatal error occurred", re.MULTILINE)
LATEX_FLAGS = ["-shell-escape", "-interaction=nonstopmode", "-file-line-error"]  # as report/Makefile


def find_pdflatex():
    """pdflatex from PATH, else from a TeX Live install under /usr/local/texlive (not on a systemd service's PATH)."""
    found = shutil.which("pdflatex")
    if found:
        return found
    installs = sorted(glob.glob("/usr/local/texlive/*/bin/*/pdflatex"))
    if installs:
        return installs[-1]  # newest year
    raise SystemExit("error: pdflatex not found (neither on PATH nor under /usr/local/texlive)")


def compile_report_pdf(tex_path):
    """Compile tex_path with pdflatex (twice, so cross-references resolve) into a private temporary directory, and
    only then replace <tex_path>.pdf with the result. Building elsewhere matters: an editor that rebuilds the
    report on every change (LaTeX Workshop runs report/Makefile in report/) would otherwise write the same
    main.aux/main.log at the same time, and one of the two builds fails ("I can't find file main.aux"). biber runs
    between the two passes (the scintillation part cites refs.bib); makeglossaries is skipped: no printed glossary.
    pdflatex's nonstopmode still writes a PDF after real LaTeX errors, so the log is checked for them too; on any
    error the existing PDF is left untouched and SystemExit is raised."""
    pdflatex = find_pdflatex()
    biber = Path(pdflatex).with_name("biber")  # from the same TeX Live install
    pdf_path = tex_path.with_suffix(".pdf")
    with tempfile.TemporaryDirectory(prefix="report-build-") as build:
        for latex_pass in range(2):
            result = subprocess.run([pdflatex, *LATEX_FLAGS, f"-output-directory={build}", tex_path.name],
                                    cwd=tex_path.parent, capture_output=True, text=True, errors="replace")
            if latex_pass == 0:
                # cwd: refs.bib is found next to main.tex; main.bcf/main.bbl live in the build directory
                bib = subprocess.run([biber if biber.exists() else "biber", f"--output-directory={build}",
                                      str(Path(build) / tex_path.stem)],
                                     cwd=tex_path.parent, capture_output=True, text=True, errors="replace")
                if bib.returncode != 0:
                    print(f"warning: biber failed, citations will show as [?]:\n{bib.stdout[-1000:]}")
        built_pdf = Path(build) / pdf_path.name
        log = (Path(build) / tex_path.with_suffix(".log").name)
        log_text = log.read_text(errors="replace") if log.exists() else ""
        log_errors = _LATEX_ERROR_RE.findall(log_text)
        if log_errors or not built_pdf.exists():
            tail = (log_text or result.stdout + result.stderr)[-2000:]
            raise SystemExit(f"error: compiling {tex_path} failed ({len(log_errors)} LaTeX error(s)); "
                             f"{pdf_path} left unchanged:\n{tail}")
        partial = pdf_path.with_name(f".{pdf_path.name}.partial")
        shutil.copyfile(built_pdf, partial)
        partial.replace(pdf_path)  # atomic: a viewer never sees a half-written PDF
    return pdf_path


def _day_ordinal(session, all_sessions):
    """Which recording of the day this session is (1st, 2nd, ...), counted across every
    session on disk for that day -- not just the batch being appended this run. Needed
    because update_report.py runs once per recording (not just once per 15-day digest), so
    a same-day counter reset per invocation would mislabel every run after the first."""
    day = session["datetime"].date()
    same_day_up_to_now = [s for s in all_sessions
                           if s["datetime"].date() == day and s["datetime"] <= session["datetime"]]
    return len(same_day_up_to_now)


def append_to_report(sessions, tex_path, all_sessions):
    """Append one \\section per day (each holding a \\subsection per recording that
    day) to tex_path's "Temporary GNSS data" \\part, just before PERMANENT_PART_MARKER. all_sessions (every session
    on disk, not just the ones being appended) is what makes the First/Second/... ordinal
    correct across several update_report.py runs on the same day.

    New sessions always arrive in non-decreasing date order (sessions is sorted by
    datetime), so only the LAST "Records on ..." section heading already in the
    document is ever relevant: if it's the same day as the next new session, that
    session's subsection is appended under it with no new heading; otherwise a new
    day section is started. This avoids ever splitting one day's recordings across
    two separate \\section blocks just because update_report.py runs once per recording
    instead of once per day."""
    if not sessions:
        return
    if not tex_path.exists():
        print(f"warning: {tex_path} not found, skipping LaTeX report update")
        return

    text = tex_path.read_text()
    marker = PERMANENT_PART_MARKER
    if marker not in text:
        print(f"warning: no {marker!r} found in {tex_path}, skipping LaTeX report update")
        return
    existing_days = re.findall(r"\\section\{Records on (\d{2}/\d{2}/\d{4})\}", text[:text.index(marker)])
    current_day = existing_days[-1] if existing_days else None

    blocks = []
    for s in sessions:
        date_str = s["datetime"].strftime("%d/%m/%Y")
        if date_str != current_day:
            blocks.append(f"\\section{{Records on {date_str}}}")
            current_day = date_str
        blocks.append(session_subsection_tex(s, _day_ordinal(s, all_sessions), tex_path.parent))

    insertion = "\n" + "\n\n".join(blocks) + "\n\n"
    text = text.replace(marker, insertion + marker, 1)
    tex_path.write_text(text)
    print(f"appended {len(sessions)} recording(s) to {tex_path}")


_DAY_SECTION_RE = re.compile(r"^\\section\{Records on (\d{2}/\d{2}/\d{4})\}$", re.MULTILINE)


def prune_old_days(tex_path, keep_days=KEPT_RECORDING_DAYS):
    """Drop every "Records on dd/mm/yyyy" \\section but the keep_days most recent ones from tex_path, together with the
    GNSS-SDR figures and tables (gnss_sdr/<name>/) its recordings \\input. Each day section runs up to the next one,
    or to PERMANENT_PART_MARKER for the last; the permanent part after it is never touched. Returns the removed
    days."""
    if not tex_path.exists():
        return []
    text = tex_path.read_text()
    temporary_end = text.find(PERMANENT_PART_MARKER)
    starts = list(_DAY_SECTION_RE.finditer(text, 0, temporary_end if temporary_end >= 0 else len(text)))
    if len(starts) <= keep_days:
        return []
    cut_from, cut_to = starts[0].start(), starts[-keep_days].start()
    removed = text[cut_from:cut_to]
    for name in sorted(set(re.findall(r"\\input\{gnss_sdr/([^/}]+)/", removed))):
        shutil.rmtree(tex_path.parent / "gnss_sdr" / name, ignore_errors=True)
    tex_path.write_text(text[:cut_from] + text[cut_to:])
    days = [m.group(1) for m in starts[:-keep_days]]
    print(f"dropped {len(days)} recording day(s) older than the last {keep_days} from {tex_path}: {', '.join(days)}")
    return days


def calendar_tikz_block(year, month, day_counts):
    """Build one calendar-month \\tikzpicture, matching the hand-written original's style:
    a day is filled green if 2 recordings happened that day, blue if 1, left uncolored
    if 0. day_counts: {date: count}, only entries for this (year, month) are used."""
    month_str = f"{year:04d}-{month:02d}"
    highlighted = sorted(d for d, c in day_counts.items()
                          if d.year == year and d.month == month and c > 0)

    fill_lines, anchor_lines = [], []
    for day in highlighted:
        color = "green" if day_counts[day] >= 2 else "blue"
        fill_lines.append(
            f"          \\ifdate{{equals={day.isoformat()}}}{{%\n"
            f"            \\tikzset{{every day/.append style={{fill={color}!40, text=blue, "
            f"shape=rectangle, inner sep=2pt}}}}%\n"
            f"          }}{{}}%"
        )
        anchor_lines.append(
            f"          \\ifdate{{equals={day.isoformat()}}}{{%\n"
            f"            \\tikzset{{every day/.style={{anchor=base east}}}}%\n"
            f"          }}{{}}%"
        )

    return "\n".join([
        r"    \begin{tikzpicture}[day xshift=3ex, day yshift=3ex]",
        r"    % month title and weekday-initials header, placed manually (one calendar",
        "    % \"row\" apart each) since the built-in month label collides with a header row",
        f"    \\calendar[dates={month_str}-01 to {month_str}-last,",
        r"        week list,",
        r"        month label above centered,",
        r"        day xshift = 0.6cm,",
        r"        day headings=orange,",
        r"        day letter headings,",
        r"        execute before day scope={%",
        *fill_lines,
        r"        },",
        r"        execute after day scope={%",
        *anchor_lines,
        r"        }]",
        r"        ;",
        r"    %   if (Sunday) [red]",
        r"    %   if (Saturday) [red];",
        r"  \end{tikzpicture}",
    ])


def _calendar_table_re(caption):
    """The table whose caption is caption, never spanning another table (one \\begin...\\end{table} pair): the
    temporary calendar comes after many metadata tables."""
    return re.compile(r"\\begin\{table\}\[H\](?:(?!\\end\{table\}).)*?" + re.escape(caption)
                      + r".*?\\end\{table\}", re.DOTALL)


def calendar_table_tex(sessions, caption, label):
    """Build the whole calendar table: one \\tikzpicture per (year, month) that has at
    least one recording, 3 per row (matching the hand-written original's ccc layout),
    padding any trailing empty cells with '% PLACEHOLDER' like the original did. Without any session, a one-line
    note with the same caption, so that the next update still finds the table."""
    day_counts = defaultdict(int)
    for s in sessions:
        day_counts[s["datetime"].date()] += 1

    months = sorted({(d.year, d.month) for d in day_counts})
    blocks = [calendar_tikz_block(year, month, day_counts) for year, month in months]

    rows = []
    for i in range(0, len(blocks), 3):
        cells = blocks[i:i + 3]
        while len(cells) < 3:
            cells.append("  % PLACEHOLDER")
        rows.append("\n  &\n".join(cells) + "\n  \\\\[2ex]")
    if not rows:
        rows = [r"  \multicolumn{3}{c}{\textit{No recording stored yet.}} \\[2ex]"]

    return "\n".join([
        r"\begin{table}[H]",
        r"  \centering",
        r"  \begin{tabular}{ccc}",
        "\n".join(rows),
        r"  \end{tabular}",
        # the colours are explained in the text above the table, which refers to it by this label
        f"  \\caption{{{caption}}}",
        f"  \\label{{{label}}}",
        r"\end{table}",
    ])


def update_calendar_table(tex_path, sessions, caption, label):
    """Replace the existing calendar table captioned caption in tex_path with a freshly
    generated one covering every month of sessions with at least one recording. Uses a plain slice
    replacement (not re.sub's replacement string) since the generated LaTeX is full of
    backslashes that re.sub would otherwise try to interpret as backreferences."""
    if not tex_path.exists():
        return
    text = tex_path.read_text()
    m = _calendar_table_re(caption).search(text)
    if not m:
        print(f"warning: no {caption!r} table found in {tex_path}, skipping calendar update")
        return
    text = text[:m.start()] + calendar_table_tex(sessions, caption, label) + text[m.end():]
    tex_path.write_text(text)
    print(f"updated {caption!r} table in {tex_path}")
