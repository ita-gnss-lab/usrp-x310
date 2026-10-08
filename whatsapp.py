"""Shared helpers for sending WhatsApp messages through a self-hosted OpenWA
gateway (https://github.com/rmyndharis/OpenWA). Used by both notify_whatsapp.py
(single-recording summary) and report_summary.py (periodic aggregate report).
"""

import base64
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


def send_document(base_url, api_key, session, to, file_path, filename=None, mimetype="application/pdf", caption=None):
    """POST a local file to OpenWA's send-document endpoint, base64-encoded in the JSON body.

    OpenWA's body-size limit (default 25 MB) caps base64 payloads to roughly
    18 MB of actual file content; this raises SystemExit before even trying
    if the file is too large, rather than letting the server reject it.
    """
    MAX_BYTES = 18 * 1024 * 1024
    data = file_path.read_bytes()
    if len(data) > MAX_BYTES:
        raise SystemExit(
            f"error: {file_path} is {len(data) / 1e6:.1f} MB, over OpenWA's ~{MAX_BYTES / 1e6:.0f} MB "
            "base64 body limit; send by URL instead or raise OpenWA's BODY_SIZE_LIMIT"
        )

    url = f"{base_url.rstrip('/')}/api/sessions/{session}/messages/send-document"
    payload = {
        "chatId": to_chat_id(to),
        "base64": base64.b64encode(data).decode("ascii"),
        "mimetype": mimetype,
        "filename": filename or file_path.name,
    }
    if caption:
        payload["caption"] = caption
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
