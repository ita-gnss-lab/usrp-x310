"""Add one recording to the LaTeX report and recompile it, right after the recording (and its GNSS-SDR processing).

Called by systemd/x310-record.sh after every recording. For the given <name>.sigmf archive it:
  1. appends the recording to report/main.tex's "Temporary GNSS data" part: a \\section{Records on dd/mm/yyyy}
     when it is the day's first recording, then a \\subsection{First/Second record} with its metadata table and,
     when gnss_sdr_out2tex.py has written them, its GNSS-SDR results (report/gnss_sdr/<name>/results.tex);
  2. keeps only the last KEPT_RECORDING_DAYS (15) recording days in that part, deleting the older days' sections
     and their report/gnss_sdr/<name>/ results, so the PDF stays small enough for WhatsApp;
  3. refreshes the temporary recording calendar (opening that part) from the archives of --recordings-dir in those
     15 days, and the permanent recording calendar (opening the "Permanent scintillating data" part) from the
     archives of --permanent-dir (none yet: the permanent storage of scintillation-corrupted recordings is not
     automated);
  4. compiles the report (pdflatex, biber, pdflatex) into report/main.pdf.
A recording already in the report is not added twice (step 1 is skipped, steps 2-4 still run). The periodic
WhatsApp message (send_whatsapp_msg.py) then only sends the compiled PDF.

--missing adds, oldest first, every recording of --recordings-dir within the last 15 recording days that is not in
the report yet: a backfill for
recordings made before this script ran after each recording (until 22/10/2026 the report was only extended every
15 days), or while it failed.

Examples:
  uv run update_report.py recordings/x310_20261021T193000Z.sigmf
  uv run update_report.py --missing
"""

import argparse
from pathlib import Path

from report_lib import (KEPT_RECORDING_DAYS, PERMANENT_CALENDAR_CAPTION, PERMANENT_CALENDAR_LABEL,
                        TEMPORARY_CALENDAR_CAPTION, TEMPORARY_CALENDAR_LABEL, append_to_report, compile_report_pdf,
                        load_sessions, prune_old_days, tex_escape, update_calendar_table)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("archive", type=Path, nargs="?", help="the recording's .sigmf archive")
    p.add_argument("--missing", action="store_true",
                   help="instead of one archive, add every recording of --recordings-dir not in the report yet")
    p.add_argument("--recordings-dir", type=Path, default=Path("recordings"),
                   help="directory holding every recording (for the day's First/Second numbering and the calendar)")
    p.add_argument("--permanent-dir", type=Path, default=None,
                   help="directory of the scintillation-corrupted recordings kept permanently (permanent calendar)")
    p.add_argument("--tex-path", type=Path, default=Path("report/main.tex"), help="the LaTeX report")
    return p.parse_args()


def in_report(tex_path, name):
    return f"Metadata for recording \\texttt{{{tex_escape(name)}}}" in tex_path.read_text()


def main():
    args = parse_args()
    if (args.archive is None) == (not args.missing):
        raise SystemExit("error: give one archive, or --missing")
    sessions = load_sessions(args.recordings_dir)
    if args.missing:
        # load_sessions sorts them by date; older days would only be dropped again by prune_old_days
        recent_days = sorted({s["datetime"].date() for s in sessions})[-KEPT_RECORDING_DAYS:]
        new = [s for s in sessions if s["datetime"].date() in recent_days and not in_report(args.tex_path, s["name"])]
    else:
        name = args.archive.name.removesuffix(".sigmf")
        if not any(s["name"] == name for s in sessions):
            # e.g. left in staging because the NAS was not mounted: still part of the report
            sessions = sorted(sessions + load_sessions(args.archive.parent), key=lambda s: s["datetime"])
            sessions = list({s["name"]: s for s in sessions}.values())
        new = [s for s in sessions if s["name"] == name]
        if not new:
            raise SystemExit(f"error: {args.archive} is not a readable recording")
        if in_report(args.tex_path, name):
            print(f"{name} is already in {args.tex_path}")
            new = []
    for session in new:  # one at a time: each may start a new day section
        append_to_report([session], args.tex_path, sessions)
    prune_old_days(args.tex_path)
    recent_days = sorted({s["datetime"].date() for s in sessions})[-KEPT_RECORDING_DAYS:]
    update_calendar_table(args.tex_path, [s for s in sessions if s["datetime"].date() in recent_days],
                          TEMPORARY_CALENDAR_CAPTION, TEMPORARY_CALENDAR_LABEL)
    permanent = load_sessions(args.permanent_dir) if args.permanent_dir and args.permanent_dir.is_dir() else []
    update_calendar_table(args.tex_path, permanent, PERMANENT_CALENDAR_CAPTION, PERMANENT_CALENDAR_LABEL)
    print(f"compiled {compile_report_pdf(args.tex_path)}")


if __name__ == "__main__":
    main()
