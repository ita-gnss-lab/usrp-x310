"""Send a periodic WhatsApp report of every recording since the last report:
a short text digest plus the report PDF (report/main.pdf).

Scans --out-dir for every <name>.sigmf Archive (x310usrp_record.py's output:
one Archive per session, bundling a Collection + each channel's Recording)
and groups each one into a session (one per x310usrp_record.py run,
multiple channels each -- a dual-channel recording counts as ONE
session/record). Keeps only the ones newer than the last report (tracked in
a state file, defaulting to 15 days back on first run), and:
  1. sends a short text summary -- counts, data volume, dropped samples,
     and which days (if any) had no recording -- through a self-hosted
     OpenWA gateway (https://github.com/rmyndharis/OpenWA).
  2. sends the report PDF as a WhatsApp document to the same recipient. The report itself is kept up to date by
     the recording service, not here: after every recording, systemd/x310-record.sh processes it with GNSS-SDR
     and runs update_report.py, which appends the recording (metadata table + GNSS-SDR results) to --tex-path
     and recompiles it. This script only recompiles when the PDF is missing or older than --tex-path.
Both sends (and the marker advance) are skipped on --dry-run.

The recordings' metadata and the PDF compilation come from report_lib.py.

Intended to run every 15 days via x310-report.timer (see systemd/). It sends
from whichever number was paired into $OPENWA_SESSION.

Example:
  export OPENWA_API_KEY=xxxxxxxx
  export OPENWA_SESSION=<uuid>
  export REPORT_WHATSAPP_TO=+55831234567   # the recipient, e.g. the lab head
  uv run send_whatsapp_msg.py
"""

import argparse
import base64
import json
import os
import re
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from report_lib import compile_report_pdf, load_sessions

DEFAULT_LOOKBACK_DAYS = 15
# OpenWA's body-size limit (default 25 MB) caps base64 payloads to roughly 18 MB of actual file content
MAX_DOCUMENT_BYTES = 18 * 1024 * 1024


# ----------------------------------------------------------------------------------------- OpenWA gateway
def to_chat_id(phone):
    """E.164 phone number ('+5581999999999') -> OpenWA chat id ('5581999999999@c.us')."""
    digits = re.sub(r"\D", "", phone)
    if not digits:
        raise SystemExit(f"error: '{phone}' has no digits to build a chat id from")
    return f"{digits}@c.us"


def post_openwa(base_url, api_key, session, endpoint, payload):
    """POST a JSON payload to one of OpenWA's message endpoints. Raises SystemExit with the server's error on
    failure."""
    url = f"{base_url.rstrip('/')}/api/sessions/{session}/messages/{endpoint}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"error: OpenWA returned {e.code}: {e.read().decode()}")
    except urllib.error.URLError as e:
        raise SystemExit(f"error: couldn't reach OpenWA at {url}: {e.reason}")


def send_whatsapp(base_url, api_key, session, to, body):
    """Send a text message."""
    return post_openwa(base_url, api_key, session, "send-text", {"chatId": to_chat_id(to), "text": body})


def send_document(base_url, api_key, session, to, file_path, filename=None, mimetype="application/pdf", caption=None):
    """Send a local file as a document, base64-encoded in the JSON body. Raises SystemExit before even trying
    if the file is over OpenWA's body limit, rather than letting the server reject it."""
    data = file_path.read_bytes()
    if len(data) > MAX_DOCUMENT_BYTES:
        raise SystemExit(
            f"error: {file_path} is {len(data) / 1e6:.1f} MB, over OpenWA's ~{MAX_DOCUMENT_BYTES / 1e6:.0f} MB "
            "base64 body limit; send by URL instead or raise OpenWA's BODY_SIZE_LIMIT"
        )
    payload = {
        "chatId": to_chat_id(to),
        "base64": base64.b64encode(data).decode("ascii"),
        "mimetype": mimetype,
        "filename": filename or file_path.name,
    }
    if caption:
        payload["caption"] = caption
    return post_openwa(base_url, api_key, session, "send-document", payload)



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
                   help="LaTeX report whose PDF is sent (default: report/main.tex)")
    return p.parse_args()


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

    if selected:
        pdf_path = args.tex_path.with_suffix(".pdf")
        if not pdf_path.exists() or pdf_path.stat().st_mtime < args.tex_path.stat().st_mtime:
            pdf_path = compile_report_pdf(args.tex_path)  # e.g. main.tex edited by hand since the last recording
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
