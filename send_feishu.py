#!/usr/bin/env python3
"""Send a report (read from stdin) to Feishu, as an interactive card.

Two delivery backends:
  - webhook : a group "custom bot" incoming webhook (preferred for a shared/company
              group). Configure with --set-webhook. Supports signed webhooks.
  - app     : a 1:1 DM via the openclaw bot app to the paired user's open_id (fallback).

Default target = webhook if one is configured, else app. Override with --target.

The report is an interactive card with a blue "Daily Report — <date>" header (a leading
"# Title" line is lifted into that header) and Feishu renders the body markdown (bold, inline
code, bullets). It renders cleanly in the Feishu chat; to paste into a Feishu Doc with bold
intact, copy from the Feishu DESKTOP client (the web client drops bold), or import the .md.

    printf '%s' "$REPORT" | python3 send_feishu.py --title "Daily Report — 2026-06-01"

Setup the group webhook (reads from stdin: line1=URL, line2=secret-or-blank):
    python3 send_feishu.py --set-webhook
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
WEBHOOK_STORE = os.path.join(SKILL_DIR, ".feishu_webhook.json")
OPENCLAW = "/home/thomas/.openclaw"
BASE = "https://open.feishu.cn"


# Feishu is reachable DIRECTLY on this host (general internet needs the corp proxy).
# Bypass the proxy so the source IP is this host's stable public IP (14.103.11.105),
# not the proxy's rotating egress — that's the IP to put on the bot's IP allowlist.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------------------
def _http_json(url, payload, headers=None):
    h = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h)
    try:
        return json.load(_OPENER.open(req, timeout=20))
    except urllib.error.HTTPError as e:
        return json.load(e)


def build_card(title, body):
    # One lark_md div: Feishu renders the markdown (bold, inline code, bullets). Looks best in
    # the Feishu chat. Pasting into a Feishu Doc keeps the bold only from the DESKTOP client (the
    # web client drops it); for a guaranteed-faithful copy, import the .md into Feishu Drive.
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": title}},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
    }


# ---------------------------------------------------------------------------
# webhook backend (group custom bot)
# ---------------------------------------------------------------------------
def load_webhook():
    if os.environ.get("FEISHU_WEBHOOK_URL"):
        return {"url": os.environ["FEISHU_WEBHOOK_URL"],
                "secret": os.environ.get("FEISHU_WEBHOOK_SECRET", "")}
    if os.path.exists(WEBHOOK_STORE):
        return json.load(open(WEBHOOK_STORE))
    return None


def set_webhook_from_stdin():
    print("Paste webhook URL on line 1, signing secret on line 2 (blank if none):", file=sys.stderr)
    lines = sys.stdin.read().splitlines()
    url = next((l.strip() for l in lines if l.strip()), "")
    if not url.startswith("http"):
        raise SystemExit("first non-empty line must be the webhook URL")
    secret = ""
    seen_url = False
    for l in lines:
        if l.strip() == url:
            seen_url = True
            continue
        if seen_url and l.strip():
            secret = l.strip()
            break
    cfg = {"url": url, "secret": secret}
    with open(WEBHOOK_STORE, "w") as f:
        json.dump(cfg, f)
    os.chmod(WEBHOOK_STORE, 0o600)
    print(f"OK: stored at {WEBHOOK_STORE} (secret: {'yes' if secret else 'none'})")


def _sign(timestamp, secret):
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send_webhook(cfg, title, body):
    card = build_card(title, body)
    # custom-keyword bots require the keyword string to appear in the message; append it as a
    # small footer note (kept OUTSIDE the body so a copied report stays clean).
    kw = cfg.get("keyword")
    if kw and kw not in (title + "\n" + body):
        card["elements"].append({"tag": "note", "elements": [{"tag": "plain_text", "content": kw}]})
    payload = {"msg_type": "interactive", "card": card}
    if cfg.get("secret"):
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _sign(ts, cfg["secret"])
    r = _http_json(cfg["url"], payload)
    ok = r.get("code") == 0 or r.get("StatusCode") == 0
    return ok, r


# ---------------------------------------------------------------------------
# app backend (1:1 DM via openclaw bot)
# ---------------------------------------------------------------------------
def app_creds():
    cfg = json.load(open(os.path.join(OPENCLAW, "openclaw.json")))["channels"]["feishu"]
    return cfg["appId"], cfg["appSecret"]


def user_open_id():
    if os.environ.get("FEISHU_OPEN_ID"):
        return os.environ["FEISHU_OPEN_ID"]
    ids = json.load(open(os.path.join(OPENCLAW, "credentials", "feishu-default-allowFrom.json"))).get("allowFrom") or []
    if not ids:
        raise SystemExit("no open_id (set FEISHU_OPEN_ID)")
    return ids[0]


def tenant_token():
    aid, sec = app_creds()
    r = _http_json("%s/open-apis/auth/v3/tenant_access_token/internal" % BASE,
                   {"app_id": aid, "app_secret": sec})
    tok = r.get("tenant_access_token")
    if not tok:
        raise SystemExit(f"token failed: {r}")
    return tok


def send_app(open_id, title, body):
    token = tenant_token()
    url = "%s/open-apis/im/v1/messages?receive_id_type=open_id" % BASE
    r = _http_json(url, {"receive_id": open_id, "msg_type": "interactive",
                         "content": json.dumps(build_card(title, body))},
                   headers={"Authorization": "Bearer " + token})
    if r.get("code") == 0:
        return True, r
    text = (title + "\n\n" + body) if title else body
    r = _http_json(url, {"receive_id": open_id, "msg_type": "text",
                         "content": json.dumps({"text": text})},
                   headers={"Authorization": "Bearer " + token})
    return r.get("code") == 0, r


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="Daily Report")
    ap.add_argument("--target", choices=["webhook", "app"], default=None,
                    help="force a backend (default: webhook if configured, else app)")
    ap.add_argument("--set-webhook", action="store_true", help="store group webhook (stdin)")
    ap.add_argument("--open-id", default=None, help="override DM recipient open_id (app target)")
    args = ap.parse_args()

    if args.set_webhook:
        set_webhook_from_stdin()
        return

    body = sys.stdin.read().strip()
    if not body:
        raise SystemExit("nothing on stdin to send")
    title = args.title
    # Lift a leading "# Title" into the blue card header so the body doesn't duplicate it.
    if body.startswith("# "):
        first, _, rest = body.partition("\n")
        title, body = first[2:].strip(), rest.strip()

    webhook = load_webhook()
    target = args.target or ("webhook" if webhook else "app")
    if target == "webhook":
        if not webhook:
            raise SystemExit("no webhook configured (run --set-webhook)")
        ok, r = send_webhook(webhook, title, body)
    else:
        ok, r = send_app(args.open_id or user_open_id(), title, body)

    print(f"send[{target}] -> code:", r.get("code", r.get("StatusCode")), "| msg:", r.get("msg", r.get("StatusMessage", "")))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
