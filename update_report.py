"""Add one recording to the LaTeX report and recompile it, right after the recording (and its GNSS-SDR processing).

Called by systemd/x310-record.sh after every recording. For the given <name>.sigmf archive it:
  1. appends the recording to report/main.tex's "Recording reports" part: a \\section{Records on dd/mm/yyyy}
     when it is the day's first recording, then a \\subsection{First/Second record} with its metadata table and,
     when gnss_sdr_out2tex.py has written them, its GNSS-SDR results (report/gnss_sdr/<name>/results.tex);
  2. refreshes the recording calendar table from every archive in --recordings-dir;
  3. compiles the report (make compile, twice) into report/main.pdf.
A recording already in the report is not added twice (step 1 is skipped, steps 2-3 still run). The periodic
WhatsApp message (send_whatsapp_msg.py) then only sends the compiled PDF.

--missing adds, oldest first, every recording of --recordings-dir that is not in the report yet: a backfill for
recordings made before this script ran after each recording (until 22/10/2026 the report was only extended every
15 days), or while it failed.

Examples:
  uv run update_report.py recordings/x310_20261021T193000Z.sigmf
  uv run update_report.py --missing
"""

import argparse
from pathlib import Path

from report_lib import append_to_report, compile_report_pdf, load_sessions, tex_escape, update_calendar_table


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("archive", type=Path, nargs="?", help="the recording's .sigmf archive")
    p.add_argument("--missing", action="store_true",
                   help="instead of one archive, add every recording of --recordings-dir not in the report yet")
    p.add_argument("--recordings-dir", type=Path, default=Path("recordings"),
                   help="directory holding every recording (for the day's First/Second numbering and the calendar)")
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
        new = [s for s in sessions if not in_report(args.tex_path, s["name"])]  # load_sessions sorts them by date
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
    update_calendar_table(args.tex_path, sessions)
    print(f"compiled {compile_report_pdf(args.tex_path)}")


if __name__ == "__main__":
    main()
