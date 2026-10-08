"""Shared helpers for sending WhatsApp messages through a self-hosted OpenWA
gateway (https://github.com/rmyndharis/OpenWA). Used by both notify_whatsapp.py
(single-recording summary) and report_summary.py (periodic aggregate report).
"""

import json
import re
import urllib.error
import urllib.request


def to_chat_id(phone):
    """E.164 phone number ('+5581999999999') -> OpenWA chat id ('5581999999999@c.us')."""
    digits = re.sub(r"\D", "", phone)
    if not digits:
        raise SystemExit(f"error: '{phone}' has no digits to build a chat id from")
    return f"{digits}@c.us"


def send_whatsapp(base_url, api_key, session, to, body):
    """POST to OpenWA's send-text endpoint. Raises SystemExit with the server's error body on failure."""
    url = f"{base_url.rstrip('/')}/api/sessions/{session}/messages/send-text"
    payload = json.dumps({"chatId": to_chat_id(to), "text": body}).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"error: OpenWA returned {e.code}: {e.read().decode()}")
    except urllib.error.URLError as e:
        raise SystemExit(f"error: couldn't reach OpenWA at {url}: {e.reason}")
