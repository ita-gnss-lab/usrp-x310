"""Send a WhatsApp message summarizing one main.py recording, via OpenWA.

Reads the <name>_ch<k>.sigmf-meta/.sigmf-data files a recording produced,
builds a short text summary (frequency, rate, gain, duration, size, dropped
samples per channel), and posts it through a self-hosted OpenWA gateway
(https://github.com/rmyndharis/OpenWA) using only the standard library.

OpenWA is unofficial WhatsApp-Web automation, not Meta's API: its own README
warns of a real account-ban risk and recommends a dedicated number rather
than your main one.

One-time setup (see the OpenWA README for the up-to-date version):
  git clone https://github.com/rmyndharis/OpenWA && cd OpenWA
  docker compose -f docker-compose.dev.yml up -d
  docker exec openwa-api cat /app/data/.api-key   # -> OPENWA_API_KEY

  # The "name" below is just a label; OpenWA assigns its own UUID as the
  # real session id (the "id" field in the response) -- that UUID is what
  # goes in every later call, including --session / $OPENWA_SESSION.
  curl -X POST http://localhost:2785/api/sessions -H "X-API-Key: $OPENWA_API_KEY" \
       -H "Content-Type: application/json" -d '{"name": "x310-notifier"}'
  curl -X POST http://localhost:2785/api/sessions/<uuid>/start -H "X-API-Key: $OPENWA_API_KEY"
  curl http://localhost:2785/api/sessions/<uuid>/qr -H "X-API-Key: $OPENWA_API_KEY"
  # scan the QR with the WhatsApp account you want to send FROM

Example:
  export OPENWA_API_KEY=xxxxxxxx
  export OPENWA_SESSION=<uuid>
  uv run main.py --name test1 --duration 5
  uv run notify_whatsapp.py --name test1 --to +5581999999999

See report_summary.py for the periodic multi-recording digest instead of a
single-recording notification.
"""

import argparse
import json
import os
import re
from pathlib import Path

from whatsapp import send_whatsapp

BYTES_PER_SAMPLE = 4  # ci16_le: int16 I + int16 Q


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", required=True, help="recording file prefix, as passed to main.py --name")
    p.add_argument("--out-dir", type=Path, default=Path("recordings"), help="directory main.py wrote into")
    p.add_argument("--to", required=True, help="recipient phone number, e.g. +5581999999999")
    p.add_argument("--base-url", default="http://localhost:2785", help="OpenWA server URL")
    p.add_argument("--session", default=None,
                   help="OpenWA session UUID, not its label (default: $OPENWA_SESSION)")
    p.add_argument("--api-key", default=None, help="OpenWA API key (default: $OPENWA_API_KEY)")
    return p.parse_args()


def load_channels(out_dir, name):
    """Return (channel_index, meta_dict, data_path) for each channel of this recording, sorted by channel."""
    channels = []
    for meta_path in sorted(out_dir.glob(f"{name}_ch*.sigmf-meta")):
        m = re.search(r"_ch(\d+)\.sigmf-meta$", meta_path.name)
        data_path = meta_path.with_suffix(".sigmf-data")
        channels.append((int(m.group(1)), json.loads(meta_path.read_text()), data_path))
    if not channels:
        raise SystemExit(f"error: no {name}_ch*.sigmf-meta files found in {out_dir}")
    return sorted(channels, key=lambda c: c[0])


def summarize(name, channels):
    """Build a short multi-line WhatsApp-friendly summary from sigmf-meta + file contents."""
    lines = [f'USRP X310 recording "{name}" complete']
    for ch, meta, data_path in channels:
        g, cap = meta["global"], meta["captures"][0]
        rate = g["core:sample_rate"]
        size = data_path.stat().st_size if data_path.exists() else 0
        duration = size / BYTES_PER_SAMPLE / rate if rate else 0.0
        lines.append(
            f"ch{ch}: {cap['core:frequency'] / 1e6:.6f} MHz, {rate / 1e6:.3f} MS/s, "
            f"gain {g['x310:gain_db']:.1f} dB, {duration:.1f} s, {size / 1e6:.1f} MB, "
            f"dropped {g['x310:dropped_samples']} samples"
        )
    lines.append(f"started {channels[0][1]['captures'][0]['core:datetime']}")
    return "\n".join(lines)


def main():
    args = parse_args()
    api_key = args.api_key or os.environ.get("OPENWA_API_KEY")
    if not api_key:
        raise SystemExit("error: set --api-key or $OPENWA_API_KEY")
    session = args.session or os.environ.get("OPENWA_SESSION")
    if not session:
        raise SystemExit("error: set --session or $OPENWA_SESSION to the session's UUID (see --help)")

    channels = load_channels(args.out_dir, args.name)
    body = summarize(args.name, channels)
    print(body)

    result = send_whatsapp(args.base_url, api_key, session, args.to, body)
    print(f"sent: {result}")


if __name__ == "__main__":
    main()
