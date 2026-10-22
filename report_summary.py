"""Send a periodic WhatsApp report of every recording since the last report,
compiled as a PDF from report/main.tex, plus a short text digest.

Scans --out-dir for every <name>.sigmf Archive (x310usrp_record.py's output:
one Archive per session, bundling a Collection + each channel's Recording)
and groups each one into a session (one per x310usrp_record.py run,
multiple channels each -- a dual-channel recording counts as ONE
session/record). Keeps only the ones newer than the last report (tracked in
a state file, defaulting to 15 days back on first run), and:
  1. sends a short text summary -- counts, data volume, dropped samples,
     and which days (if any) had no recording -- through a self-hosted
     OpenWA gateway (https://github.com/rmyndharis/OpenWA). See
     notify_whatsapp.py for a single-recording notification instead of this
     periodic aggregate.
  2. appends one \\subsection{Records on dd/mm/yyyy} per day (holding one
     \\subsubsection{First/Second/... record} per session, each a metadata
     table) into --tex-path's "Recording reports" section (just before
     \\end{document}).
  3. compiles --tex-path (via `make compile` in its directory, reusing the
     report's own Makefile/build toolchain) and sends the resulting PDF as
     a WhatsApp document to the same recipient.
Steps 2-3 (and the marker advance) are skipped together on --dry-run, so
the report file and the last-report marker never drift apart.

Intended to run every 15 days via x310-report.timer (see systemd/), driven
by the same WhatsApp session as notify_whatsapp.py -- i.e. it sends from
whichever number was paired into $OPENWA_SESSION.

Example:
  export OPENWA_API_KEY=xxxxxxxx
  export OPENWA_SESSION=<uuid>
  export REPORT_WHATSAPP_TO=+55831234567   # the recipient, e.g. the lab head
  uv run report_summary.py
"""

import argparse
import json
import os
import re
import subprocess
import tarfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from whatsapp import send_document, send_whatsapp

DEFAULT_LOOKBACK_DAYS = 15
ORDINALS = ["First", "Second", "Third", "Fourth", "Fifth"]  # per-day record count never exceeds 2 in practice


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out-dir", type=Path, default=Path("recordings"), help="directory x310usrp_record.py wrote into")
    p.add_argument("--to", default=None, help="recipient phone number (default: $REPORT_WHATSAPP_TO)")
    p.add_argument("--base-url", default="http://localhost:2785", help="OpenWA server URL")
    p.add_argument("--session", default=None,
                   help="OpenWA session UUID, not its label (default: $OPENWA_SESSION)")
    p.add_argument("--api-key", default=None, help="OpenWA API key (default: $OPENWA_API_KEY)")
    p.add_argument("--state-dir", type=Path, default=None,
                   help="where the last-report marker lives (default: $STATE_DIRECTORY or "
                        "/var/lib/x310-recorder)")
    p.add_argument("--since", default=None,
                   help="ISO datetime to report from instead of the stored marker (for manual runs)")
    p.add_argument("--dry-run", action="store_true", help="print the report but don't send it or advance the marker")
    p.add_argument("--tex-path", type=Path, default=Path("report/main.tex"),
                   help="LaTeX report to append per-recording subsections to (default: report/main.tex)")
    return p.parse_args()


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


def summarize(selected, cutoff, now):
    if not selected:
        return f"USRP X310 report: no recordings between {cutoff.date()} and {now.date()}."

    per_day = defaultdict(int)
    for s in selected:
        per_day[s["datetime"].date()] += 1

    lines = [
        f"USRP X310 recording report: {cutoff.date()} to {now.date()}",
        f"{len(selected)} recording(s) across {len(per_day)} day(s)",
        f"total data: {sum(s['bytes'] for s in selected) / 1e6:.1f} MB, "
        f"dropped samples: {sum(s['dropped'] for s in selected)}",
    ]

    first_channels = selected[0]["channels"]
    lines.append("channels:")
    for ch, meta, _ in first_channels:
        lines.append(f"  ch{ch}: {meta['captures'][0]['core:frequency'] / 1e6:.3f} MHz, "
                     f"{meta['global']['core:sample_rate'] / 1e6:.3f} MS/s, {meta['global']['x310:gain_db']:.1f} dB")

    lines.append("")
    lines.append("per-day breakdown:")
    for day in sorted(per_day):
        lines.append(f"  {day}: {per_day[day]} recording(s)")

    span_days = (now.date() - cutoff.date()).days + 1
    all_days = [cutoff.date() + timedelta(days=i) for i in range(span_days)]
    missed = [d for d in all_days if per_day.get(d, 0) == 0]
    if missed:
        lines.append("")
        lines.append(f"no recordings on: {', '.join(str(d) for d in missed)}")

    return "\n".join(lines)


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


def session_subsubsection_tex(session, record_number, tex_dir=Path("report")):
    """Build one '\\subsubsection{...}' + metadata table for a single recording session, followed by its GNSS-SDR
    results (\\input of gnss_sdr/<name>/results.tex, written by gnss_sdr_report.py after the recording was
    processed) when tex_dir has them."""
    g0 = session["channels"][0][1]["global"]
    cap0 = session["channels"][0][1]["captures"][0]
    start_str = session["datetime"].strftime("%Y-%m-%d %H:%M:%S UTC")
    sample_type = tex_escape(g0["core:sample_type_desc"])

    channel_cols = " & ".join(f"\\textbf{{Channel {ch}}}" for ch, _, _ in session["channels"])
    freq_row = " & ".join(f"{meta['captures'][0]['core:frequency'] / 1e6:.3f} MHz"
                           for _, meta, _ in session["channels"])
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
    span = f"\\multicolumn{{{n_channels}}}{{c|}}"  # centered across the channel columns
    col_spec = "|l|" + "l|" * n_channels  # vertical lines between every column

    rows = [
        f"Start time & {span}{{{start_str}}} \\\\",
        f"Duration & {duration_row} \\\\",
        f"Sample rate & {rate_row} \\\\",
        f"Sample type & {span}{{{sample_type}}} \\\\",
        f"Frequency & {freq_row} \\\\",
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

    lines = [
        f"\\subsubsection{{{ordinal(record_number)} record}}",
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
    results = Path("gnss_sdr") / session["name"] / "results.tex"
    if (tex_dir / results).exists():
        lines += ["", f"\\input{{{results.as_posix()}}}"]
    else:
        lines += ["", r"\textit{GNSS-SDR processing of this recording is not available.}"]
    return "\n".join(lines)


_LATEX_ERROR_RE = re.compile(r"^! |LaTeX Error:|Emergency stop|Fatal error occurred", re.MULTILINE)


def compile_report_pdf(tex_path):
    """Run `make compile` in tex_path's directory (the report's own Makefile/toolchain:
    pdflatex + biber + makeglossaries), twice so cross-references resolve. Two things make this document's build quirky, both
    handled here rather than by trusting make's bare exit code:
      - pdflatex's nonstopmode recovers from real LaTeX errors and still writes *a* PDF, so a
        fresh mtime alone doesn't prove success -- main.log is grepped for actual error markers.
      - this document has no \\cite{} commands, so biber/glossaries are cosmetic; biber is also
        occasionally flaky reading a main.bcf pdflatex just wrote (a filesystem-flush race),
        failing make's exit code even though the PDF itself is already complete and correct.
    So: a missing/stale PDF, or a real LaTeX error in main.log, raises SystemExit; a nonzero
    make exit code with neither of those (e.g. the biber race) is treated as success."""
    pdf_path = tex_path.with_suffix(".pdf")
    log_path = tex_path.with_suffix(".log")
    before_mtime = pdf_path.stat().st_mtime if pdf_path.exists() else None

    # twice: the first pass writes the cross-references (Section~\\ref{sec:gnss-sdr}, figures) the second resolves
    for _ in range(2):
        result = subprocess.run(["make", "compile"], cwd=tex_path.parent, capture_output=True, text=True)

    after_mtime = pdf_path.stat().st_mtime if pdf_path.exists() else None
    if after_mtime is None or after_mtime == before_mtime:
        tail = (result.stdout + result.stderr)[-2000:]
        raise SystemExit(f"error: `make compile` in {tex_path.parent} didn't produce a fresh {pdf_path}:\n{tail}")

    log_errors = _LATEX_ERROR_RE.findall(log_path.read_text(errors="replace")) if log_path.exists() else []
    if log_errors:
        raise SystemExit(f"error: {log_path} reports {len(log_errors)} LaTeX error(s) "
                          f"(e.g. {log_errors[0]!r}); fix main.tex before sending the PDF")

    if result.returncode != 0:
        print(f"warning: `make compile` exited {result.returncode} (likely biber/glossaries "
              f"post-processing, not needed by this citation-less document) but {pdf_path} "
              f"was freshly rebuilt with no errors in {log_path}; continuing")
    return pdf_path


def _day_ordinal(session, all_sessions):
    """Which recording of the day this session is (1st, 2nd, ...), counted across every
    session on disk for that day -- not just the batch being appended this run. Needed
    because this script can run once per recording (not just once per 15-day digest), so
    a same-day counter reset per invocation would mislabel every run after the first."""
    day = session["datetime"].date()
    same_day_up_to_now = [s for s in all_sessions
                           if s["datetime"].date() == day and s["datetime"] <= session["datetime"]]
    return len(same_day_up_to_now)


def append_to_report(sessions, tex_path, all_sessions):
    """Append one \\subsection per day (each holding a \\subsubsection per recording that
    day) to tex_path's document, just before \\end{document}. all_sessions (every session
    on disk, not just the ones being appended) is what makes the First/Second/... ordinal
    correct across multiple runs of this script on the same day.

    New sessions always arrive in non-decreasing date order (sessions is sorted by
    datetime), so only the LAST "Records on ..." subsection heading already in the
    document is ever relevant: if it's the same day as the next new session, that
    session's subsubsection is appended under it with no new heading; otherwise a new
    day subsection is started. This avoids ever splitting one day's recordings across
    two separate \\subsection blocks just because this script ran once per recording
    instead of once per day."""
    if not sessions:
        return
    if not tex_path.exists():
        print(f"warning: {tex_path} not found, skipping LaTeX report update")
        return

    text = tex_path.read_text()
    existing_days = re.findall(r"\\subsection\{Records on (\d{2}/\d{2}/\d{4})\}", text)
    current_day = existing_days[-1] if existing_days else None

    blocks = []
    for s in sessions:
        date_str = s["datetime"].strftime("%d/%m/%Y")
        if date_str != current_day:
            blocks.append(f"\\subsection{{Records on {date_str}}}")
            current_day = date_str
        blocks.append(session_subsubsection_tex(s, _day_ordinal(s, all_sessions), tex_path.parent))

    marker = r"\end{document}"
    if marker not in text:
        print(f"warning: no {marker!r} found in {tex_path}, skipping LaTeX report update")
        return

    insertion = "\n" + "\n\n".join(blocks) + "\n\n"
    text = text.replace(marker, insertion + marker, 1)
    tex_path.write_text(text)
    print(f"appended {len(sessions)} subsection(s) to {tex_path}")


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


_CALENDAR_TABLE_RE = re.compile(r"\\begin\{table\}\[H\].*?Recording calendar\..*?\\end\{table\}", re.DOTALL)


def calendar_table_tex(all_sessions):
    """Build the whole calendar table: one \\tikzpicture per (year, month) that has at
    least one recording, 3 per row (matching the hand-written original's ccc layout),
    padding any trailing empty cells with '% PLACEHOLDER' like the original did."""
    day_counts = defaultdict(int)
    for s in all_sessions:
        day_counts[s["datetime"].date()] += 1

    months = sorted({(d.year, d.month) for d in day_counts})
    blocks = [calendar_tikz_block(year, month, day_counts) for year, month in months]

    rows = []
    for i in range(0, len(blocks), 3):
        cells = blocks[i:i + 3]
        while len(cells) < 3:
            cells.append("  % PLACEHOLDER")
        rows.append("\n  &\n".join(cells) + "\n  \\\\[2ex]")

    return "\n".join([
        r"\begin{table}[H]",
        r"  \centering",
        r"  \begin{tabular}{ccc}",
        "\n".join(rows),
        r"  \end{tabular}",
        r"  \caption{Recording calendar. \textcolor{green}{Green}: 2 recordings made; "
        r"\textcolor{blue}{blue}: 1 recording made. No color: no recording made.}",
        r"\end{table}",
    ])


def update_calendar_table(tex_path, all_sessions):
    """Replace the existing 'Recording calendar' table in tex_path with a freshly
    generated one covering every month with at least one recording. Uses a plain slice
    replacement (not re.sub's replacement string) since the generated LaTeX is full of
    backslashes that re.sub would otherwise try to interpret as backreferences."""
    if not tex_path.exists() or not all_sessions:
        return
    text = tex_path.read_text()
    m = _CALENDAR_TABLE_RE.search(text)
    if not m:
        print(f"warning: no 'Recording calendar' table found in {tex_path}, skipping calendar update")
        return
    text = text[:m.start()] + calendar_table_tex(all_sessions) + text[m.end():]
    tex_path.write_text(text)
    print(f"updated calendar table in {tex_path}")


def main():
    args = parse_args()
    api_key = args.api_key or os.environ.get("OPENWA_API_KEY")
    if not api_key:
        raise SystemExit("error: set --api-key or $OPENWA_API_KEY")
    session = args.session or os.environ.get("OPENWA_SESSION")
    if not session:
        raise SystemExit("error: set --session or $OPENWA_SESSION to the session's UUID")
    to = args.to or os.environ.get("REPORT_WHATSAPP_TO")
    if not to:
        raise SystemExit("error: set --to or $REPORT_WHATSAPP_TO")

    state_dir = args.state_dir or Path(os.environ.get("STATE_DIRECTORY", "/var/lib/x310-recorder"))
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / "last_report"

    now = datetime.now(timezone.utc)
    if args.since:
        cutoff = datetime.fromisoformat(args.since)
    elif marker.exists():
        cutoff = datetime.fromisoformat(marker.read_text().strip())
    else:
        cutoff = now - timedelta(days=DEFAULT_LOOKBACK_DAYS)

    sessions = load_sessions(args.out_dir)
    selected = [s for s in sessions if s["datetime"] > cutoff]
    body = summarize(selected, cutoff, now)
    print(body)

    if args.dry_run:
        print("--dry-run: not sending, not advancing the last-report marker")
        return

    result = send_whatsapp(args.base_url, api_key, session, to, body)
    print(f"sent: {result}")

    append_to_report(selected, args.tex_path, sessions)
    if selected:
        update_calendar_table(args.tex_path, sessions)
        pdf_path = compile_report_pdf(args.tex_path)
        print(f"compiled {pdf_path}")
        doc_result = send_document(
            args.base_url, api_key, session, to, pdf_path,
            filename="USRP_X310_Report.pdf",
            caption=f"USRP X310 recording report: {cutoff.date()} to {now.date()}",
        )
        print(f"sent document: {doc_result}")

    marker.write_text(now.isoformat())


if __name__ == "__main__":
    main()
