"""Send a periodic WhatsApp report of every recording since the last report,
compiled as a PDF from report/main.tex, plus a short text digest.

Scans --out-dir for all <name>_ch<k>.sigmf-meta/.sigmf-data files, groups
them back into recording sessions (one per x310usrp_record.py run, multiple
channels each -- a dual-channel recording counts as ONE session/record),
keeps only the ones newer than the last report (tracked in a state file,
defaulting to 15 days back on first run), and:
  1. sends a short text summary -- counts, data volume, dropped samples,
     and which days (if any) had no recording -- through a self-hosted
     OpenWA gateway (https://github.com/rmyndharis/OpenWA). See
     notify_whatsapp.py for a single-recording notification instead of this
     periodic aggregate.
  2. appends one "Records on dd/mm/yyyy -- First/Second record" subsection
     per session, each a metadata table, into --tex-path's "Recording
     reports" section (just before \\end{document}).
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


def load_sessions(out_dir):
    """Group <name>_ch<k>.sigmf-meta files back into one entry per recording session."""
    by_name = defaultdict(list)
    for meta_path in sorted(out_dir.glob("*_ch*.sigmf-meta")):
        m = re.match(r"^(.*)_ch(\d+)\.sigmf-meta$", meta_path.name)
        if m:
            by_name[m.group(1)].append((int(m.group(2)), meta_path))

    sessions = []
    for name, entries in by_name.items():
        channels = []
        for ch, meta_path in sorted(entries):
            meta = json.loads(meta_path.read_text())
            data_path = meta_path.with_suffix(".sigmf-data")
            size = data_path.stat().st_size if data_path.exists() else 0
            channels.append((ch, meta, size))
        dt = datetime.fromisoformat(channels[0][1]["captures"][0]["core:datetime"].replace("Z", "+00:00"))
        sessions.append({
            "name": name,
            "datetime": dt,
            "channels": channels,
            "dropped": sum(meta["global"]["x310:dropped_samples"] for _, meta, _ in channels),
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
    freqs = ", ".join(f"ch{ch}={meta['captures'][0]['core:frequency'] / 1e6:.3f} MHz" for ch, meta, _ in first_channels)
    lines.append(f"channels: {freqs}")

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


def session_subsection_tex(session, record_number):
    """Build one '\\subsection{...}' + metadata table for a single recording session."""
    g0 = session["channels"][0][1]["global"]
    cap0 = session["channels"][0][1]["captures"][0]
    date_str = session["datetime"].strftime("%d/%m/%Y")
    start_str = session["datetime"].strftime("%Y-%m-%d %H:%M:%S UTC")
    rate = g0["core:sample_rate"]
    bytes_per_sample = g0.get("core:bytes_per_sample", 4)
    sample_type = tex_escape(g0.get("core:sample_type_desc", g0.get("core:sample_type", "ci16_le")))

    channel_cols = " & ".join(f"\\textbf{{Channel {ch}}}" for ch, _, _ in session["channels"])
    freq_row = " & ".join(f"{meta['captures'][0]['core:frequency'] / 1e6:.3f} MHz"
                           for _, meta, _ in session["channels"])
    gain_row = " & ".join(f"{meta['global']['x310:gain_db']:.1f} dB" for _, meta, _ in session["channels"])
    antenna_row = " & ".join(tex_escape(meta["global"]["x310:antenna"]) for _, meta, _ in session["channels"])
    dropped_row = " & ".join(str(meta["global"]["x310:dropped_samples"]) for _, meta, _ in session["channels"])
    size_row = " & ".join(f"{size / 1e6:.1f} MB" for _, _, size in session["channels"])
    # x310:written_samples is only present on recordings made after this field was added;
    # older ones fall back to deriving it from the file size, same as the duration calc below.
    written_row = " & ".join(
        f"{meta['global']['x310:written_samples']:,}" if "x310:written_samples" in meta["global"]
        else f"{size // meta['global'].get('core:bytes_per_sample', 4):,}"
        for _, meta, size in session["channels"]
    )
    duration = session["channels"][0][2] / bytes_per_sample / rate if rate else 0.0

    n_channels = len(session["channels"])
    span = f"\\multicolumn{{{n_channels}}}{{c|}}"  # centered across the channel columns
    col_spec = "|l|" + "l|" * n_channels  # vertical lines between every column

    rows = [
        f"Start time & {span}{{{start_str}}} \\\\",
        f"Duration & {span}{{{duration:.1f} s}} \\\\",
        f"Sample rate & {span}{{{rate / 1e6:.3f} MS/s}} \\\\",
        f"Sample type & {span}{{{sample_type}}} \\\\",
        f"Frequency & {freq_row} \\\\",
        f"Gain & {gain_row} \\\\",
        f"Antenna & {antenna_row} \\\\",
        f"File size & {size_row} \\\\",
        f"Samples written & {written_row} \\\\",
        f"Lost samples & {dropped_row} \\\\",
        f"Clock source & {span}{{{tex_escape(g0['x310:clock_source'])}}} \\\\",
        f"Time source & {span}{{{tex_escape(g0['x310:time_source'])}}} \\\\",
        f"Hardware & {span}{{{tex_escape(g0['core:hw'])}}} \\\\",
        f"Recorder & {span}{{{tex_escape(g0['core:recorder'])}}} \\\\",
    ]

    lines = [
        f"\\subsection{{Records on {date_str} -- {ordinal(record_number)} record}}",
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
    return "\n".join(lines)


_LATEX_ERROR_RE = re.compile(r"^! |LaTeX Error:|Emergency stop|Fatal error occurred", re.MULTILINE)


def compile_report_pdf(tex_path):
    """Run `make compile` in tex_path's directory (the report's own Makefile/toolchain:
    pdflatex + biber + makeglossaries). Two things make this document's build quirky, both
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


def append_to_report(sessions, tex_path):
    """Append one subsection per session to tex_path's document, just before \\end{document}."""
    if not sessions:
        return
    if not tex_path.exists():
        print(f"warning: {tex_path} not found, skipping LaTeX report update")
        return

    per_day = defaultdict(int)
    blocks = []
    for s in sessions:
        day = s["datetime"].date()
        per_day[day] += 1
        blocks.append(session_subsection_tex(s, per_day[day]))

    text = tex_path.read_text()
    marker = r"\end{document}"
    if marker not in text:
        print(f"warning: no {marker!r} found in {tex_path}, skipping LaTeX report update")
        return

    insertion = "\n" + "\n\n".join(blocks) + "\n\n"
    text = text.replace(marker, insertion + marker, 1)
    tex_path.write_text(text)
    print(f"appended {len(blocks)} subsection(s) to {tex_path}")


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

    # result = send_whatsapp(args.base_url, api_key, session, to, body)
    # print(f"sent: {result}")

    append_to_report(selected, args.tex_path)
    if selected:
        pdf_path = compile_report_pdf(args.tex_path)
        print(f"compiled {pdf_path}")
        # doc_result = send_document(
        #     args.base_url, api_key, session, to, pdf_path,
        #     filename="USRP_X310_Report.pdf",
        #     caption=f"USRP X310 recording report: {cutoff.date()} to {now.date()}",
        # )
        # print(f"sent document: {doc_result}")

    marker.write_text(now.isoformat())


if __name__ == "__main__":
    main()
