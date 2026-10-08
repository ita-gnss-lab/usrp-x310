"""Send a periodic WhatsApp digest of every recording since the last report.

Scans --out-dir for all <name>_ch<k>.sigmf-meta/.sigmf-data files, groups
them back into recording sessions (one per main.py run, multiple channels
each), keeps only the ones newer than the last report (tracked in a state
file, defaulting to 15 days back on first run), and sends a summary --
counts, data volume, dropped samples, and which days (if any) had no
recording -- through a self-hosted OpenWA gateway
(https://github.com/rmyndharis/OpenWA). See notify_whatsapp.py for a
single-recording notification instead of this periodic aggregate.

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
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from whatsapp import send_whatsapp

DEFAULT_LOOKBACK_DAYS = 15


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out-dir", type=Path, default=Path("recordings"), help="directory main.py wrote into")
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
    marker.write_text(now.isoformat())


if __name__ == "__main__":
    main()
