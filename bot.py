# -*- coding: utf-8 -*-
"""
RF Key Hub - Telegram bot + key website (single Flask app, webhook mode)
Host: Render.com (Web Service).  Everything is hardcoded below - no env variables.

Render's free disk is wiped on restart, so ALL bot data (settings, links, 24h limits)
is stored in your Firebase Realtime Database under the node  RF_Hub/  .
Firebase is also used to create the license keys (DPMods_Security/Keys).

Flow:
  Bot -> "Get Free Key" -> personal link (shortened with VPLink, so the user must pass
  the ad) -> website opens -> key is created & saved -> the same key is shown every time
  that link is opened for 24h -> after 24h: expired. New key = wait 24h.
"""
import os
import re
import html
import time
import string
import secrets
import hashlib
import threading
import traceback
from collections import deque
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, request, jsonify, send_from_directory

# =====================================================================
#                      CONFIG  (EDIT THESE LINES)
# =====================================================================
BOT_TOKEN = "8706180742:AAHVcpLP6McesoO_eRn_0q4JgBA3PkfjUag"
ADMIN_CHAT_IDS = [8602082771]                 # your Telegram chat id(s), e.g. [111, 222]

# Your Render address (shown on the Render dashboard after you create the service)
BASE_URL = "https://rfkeyhub.onrender.com"

# Any letters/numbers/underscore. Protects the webhook URL.
WEBHOOK_SECRET = "rfkeyhub_9xQ2m7Lp"

# =====================================================================
#                      VPLINK (ad link shortener)
# =====================================================================
VPLINK_API_TOKEN = "92993639043b572cce40b27935bbdd123e5ebb66"
VPLINK_API_URL = "https://vplink.in/api"

# =====================================================================
#                      FIREBASE (admin panel login)
# =====================================================================
FIREBASE_URL = "https://rfxaim-default-rtdb.firebaseio.com"
FIREBASE_API_KEY = "AIzaSyAZDle1IozrEpOdVE9nggFB_j1ZnEzrdxE"
ADMIN_EMAIL = "ufbfahimyt250@gmail.com"
ADMIN_PASSWORD = "fahim@2507713"

# =====================================================================
#                      KEY RULES (same schema as admin panel)
# =====================================================================
KEY_PREFIX = "RF-VIP-"        # panel used DP-VIP-, now RF-VIP-
KEY_USERNAME = "Giveaway"     # "Client Name" field in the panel
KEY_DEVICE_LIMIT = 1          # "Device Limit" field
KEY_VALID_DAYS = 1            # "Expiry" = today + 1 day
COOLDOWN_HOURS = 24           # one key per Telegram ID per 24h; key page valid 24h

# --- Anti-bypass ---
TOKEN_TTL_SECONDS = 20 * 60   # the personal link must be opened within 20 minutes
MIN_AD_SECONDS = 12           # opened faster than this = ad skipped, link is cancelled

# =====================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

_lock = threading.RLock()
_seen_updates = deque(maxlen=1000)


# ---------------------------------------------------------------------
# Firebase (signs in as the admin, same as the admin panel)
# ---------------------------------------------------------------------
_tok = {"id": None, "refresh": None, "exp": 0}
_tok_lock = threading.Lock()


def _sign_in():
    r = requests.post(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "returnSecureToken": True},
        timeout=15,
    )
    d = r.json()
    if not r.ok:
        raise RuntimeError("Firebase auth failed: " + str(d.get("error", {}).get("message")))
    _tok.update(id=d["idToken"], refresh=d["refreshToken"], exp=time.time() + int(d["expiresIn"]))


def fb_token(force=False):
    with _tok_lock:
        if not force and _tok["id"] and time.time() < _tok["exp"] - 120:
            return _tok["id"]
        if not force and _tok["refresh"]:
            try:
                r = requests.post(
                    f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}",
                    data={"grant_type": "refresh_token", "refresh_token": _tok["refresh"]},
                    timeout=15,
                )
                if r.ok:
                    d = r.json()
                    _tok.update(id=d["id_token"], refresh=d["refresh_token"],
                                exp=time.time() + int(d["expires_in"]))
                    return _tok["id"]
            except Exception:
                pass
        _sign_in()
        return _tok["id"]


def fb(method, path, body=None):
    for attempt in range(2):
        token = fb_token(force=(attempt == 1))
        r = requests.request(
            method, f"{FIREBASE_URL}/{path}.json",
            params={"auth": token}, json=body, timeout=15,
        )
        if r.status_code in (401, 403) and attempt == 0:
            continue
        if not r.ok:
            raise RuntimeError(f"Firebase {method} {path} -> HTTP {r.status_code}")
        return r.json()


# Bot data lives under RF_Hub/ (separate from the license data)
def db_get(p):
    return fb("GET", f"RF_Hub/{p}")


def db_put(p, v):
    return fb("PUT", f"RF_Hub/{p}", v)


def db_patch(p, v):
    return fb("PATCH", f"RF_Hub/{p}", v)


def db_del(p):
    return fb("DELETE", f"RF_Hub/{p}")


def th(token):
    """Tokens are stored hashed, so nobody reading the database can open a link."""
    return hashlib.sha256(token.encode()).hexdigest()[:40]


def create_license_key():
    """Creates a key with exactly the admin-panel schema."""
    expiry = (datetime.now(timezone.utc) + timedelta(days=KEY_VALID_DAYS)).date().isoformat()
    alphabet = string.ascii_uppercase + string.digits
    key = None
    for _ in range(10):
        cand = KEY_PREFIX + "".join(secrets.choice(alphabet) for _ in range(6))
        if fb("GET", f"DPMods_Security/Keys/{cand}") is None:
            key = cand
            break
    if not key:
        raise RuntimeError("Could not generate a unique key")
    record = {
        "Username": KEY_USERNAME,
        "DeviceLimit": KEY_DEVICE_LIMIT,
        "ExpiryDate": expiry,
        "Banned": False,
        "Devices": {"dummy": 0},
    }
    fb("PUT", f"DPMods_Security/Keys/{key}", record)
    saved = fb("GET", f"DPMods_Security/Keys/{key}")
    if not saved or saved.get("ExpiryDate") != expiry:
        raise RuntimeError("Firebase write verification failed")
    return key, expiry


# ---------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------
def tg(method, **params):
    params = {k: v for k, v in params.items() if v is not None}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=params, timeout=15,
        )
        return r.json()
    except Exception as e:
        print("TG error:", method, e)
        return {"ok": False}


def esc(s):
    return html.escape(str(s), quote=True)


B_KEY = "🔑 Get Free Key"
B_ID = "🆔 My ID"
B_VIDEO = "🎬 Setup Video"
B_APK = "📥 Download APK"
B_SET_APK = "📦 Set APK Link"
B_SET_VID = "🎥 Set Video Link"
B_SETTINGS = "⚙️ Settings"
ALL_BUTTONS = {B_KEY, B_ID, B_VIDEO, B_APK, B_SET_APK, B_SET_VID, B_SETTINGS}

SET_MAP = {
    B_SET_APK: ("apk_link", "APK Share Link"),
    B_SET_VID: ("video_link", "Setup Video Link"),
}


def menu_markup(is_admin):
    rows = [
        [{"text": B_KEY}],
        [{"text": B_ID}, {"text": B_VIDEO}],
        [{"text": B_APK}],
    ]
    if is_admin:
        rows += [
            [{"text": B_SET_APK}, {"text": B_SET_VID}],
            [{"text": B_SETTINGS}],
        ]
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}


def send(cid, text, markup=None, **kw):
    return tg("sendMessage", chat_id=cid, text=text, parse_mode="HTML",
              reply_markup=markup, disable_web_page_preview=True, **kw)


def animated_send(cid, frames, final_text, inline=None, reply_kb=None):
    """Tiny loading animation by editing one message, then the final text."""
    tg("sendChatAction", chat_id=cid, action="typing")
    r = send(cid, frames[0])
    mid = (r.get("result") or {}).get("message_id")
    if not mid:
        send(cid, final_text, inline or reply_kb)
        return
    for f in frames[1:]:
        time.sleep(0.45)
        tg("editMessageText", chat_id=cid, message_id=mid, text=f, parse_mode="HTML")
    time.sleep(0.45)
    tg("editMessageText", chat_id=cid, message_id=mid, text=final_text,
       parse_mode="HTML", reply_markup=inline, disable_web_page_preview=True)


def fmt_wait(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h {m}m" if h else f"{max(m, 1)}m"


# ---------------------------------------------------------------------
# Bot handlers
# ---------------------------------------------------------------------
LINE = "━━━━━━━━━━━━━━━━━━"


def welcome(cid, is_admin):
    text = (
        f"✨ <b>Welcome to RF Key Hub</b> ✨\n{LINE}\n"
        "🚀 Get your <b>free key</b> in a few taps.\n"
        "🆔 Tap <b>My ID</b> to get your Chat ID.\n"
        "🎬 Watch the <b>setup video</b>.\n"
        "📥 Download the <b>APK</b>.\n"
        f"{LINE}\n👇 <i>Use the menu below</i>"
    )
    if is_admin:
        text += "\n\n👑 <b>Admin mode enabled</b>"
    send(cid, text, menu_markup(is_admin))


def show_id(cid):
    text = (
        f"🆔 <b>Your Telegram Chat ID</b>\n{LINE}\n"
        f"<code>{cid}</code>\n{LINE}\n"
        "👆 <i>Tap the ID to copy it</i>"
    )
    inline = {"inline_keyboard": [[{"text": "📋 Copy ID", "copy_text": {"text": str(cid)}}]]}
    send(cid, text, inline)


def link_button(cid, link, title, button_text, missing_text):
    if not link:
        send(cid, f"⚠️ <b>{missing_text}</b>\n<i>Please try again later.</i>")
        return
    final = f"{title}\n{LINE}\n👇 <i>Tap the button below</i>"
    inline = {"inline_keyboard": [[{"text": button_text, "url": link}]]}
    animated_send(cid, ["⏳ <b>Loading</b> ▫️▫️▫️", "⏳ <b>Loading</b> ◾▫️▫️", "⏳ <b>Loading</b> ◾◾▫️", "✅ <b>Ready</b>"],
                  final, inline)


LAST_SHORTEN_ERROR = ""


def shorten(long_url):
    """Create a VPLink short link for long_url. Returns None on failure
    (the reason is kept in LAST_SHORTEN_ERROR and sent to the admin)."""
    global LAST_SHORTEN_ERROR
    try:
        r = requests.get(
            VPLINK_API_URL,
            params={"api": VPLINK_API_TOKEN, "url": long_url, "format": "text"},
            timeout=15,
        )
        txt = r.text.strip().strip('"')
        if txt.startswith("http"):
            return txt.splitlines()[0].strip().strip('"')
        try:  # JSON fallback: {"status":"success","shortenedUrl":"https://..."}
            j = r.json()
            u = str(j.get("shortenedUrl", "")).strip().strip('"')
            if u.startswith("http"):
                return u
        except Exception:
            pass
        LAST_SHORTEN_ERROR = f"HTTP {r.status_code} | reply: {txt[:200] or '(empty)'}"
    except Exception as e:
        LAST_SHORTEN_ERROR = f"{type(e).__name__}: {str(e)[:200]}"
    print("VPLINK error:", LAST_SHORTEN_ERROR)
    return None


def cooldown_left(cid):
    g = db_get(f"generated/{cid}")
    if not g:
        return 0
    left = g["ts"] + COOLDOWN_HOURS * 3600 - time.time()
    return left if left > 0 else 0


def get_free_key(cid):
    token = secrets.token_urlsafe(16)
    tk = th(token)
    with _lock:
        left = cooldown_left(cid)
        if left:
            send(cid,
                 f"⏳ <b>You already got your key</b>\n{LINE}\n"
                 f"🔁 Next key in <b>{fmt_wait(left)}</b>\n"
                 "<i>1 key per 24 hours</i>")
            return
        # remove this user's older, never-used link
        old = db_get(f"pending/{cid}")
        if old:
            o = db_get(f"tokens/{old}")
            if o and not o.get("key"):
                db_del(f"tokens/{old}")
        db_put(f"tokens/{tk}", {"cid": cid, "created": time.time()})
        db_put(f"pending/{cid}", tk)

    long_url = f"{BASE_URL.rstrip('/')}/?t={token}"
    link = shorten(long_url)
    note = ""
    if not link and cid in ADMIN_CHAT_IDS:
        link = long_url
        note = f"\n\n🛠 <i>Admin test link (no ad). VPLink error:</i>\n<code>{esc(LAST_SHORTEN_ERROR)}</code>"
    if not link:
        db_del(f"tokens/{tk}")
        db_del(f"pending/{cid}")
        for a in ADMIN_CHAT_IDS:
            send(a, f"🚨 <b>VPLink failed</b>\n{LINE}\n<code>{esc(LAST_SHORTEN_ERROR)}</code>\n"
                    f"👤 User: <code>{cid}</code>")
        send(cid, "⚠️ <b>Key page is not available right now</b>\n<i>Please try again in a minute.</i>")
        return

    final = (
        f"🔑 <b>Get Your Free Key</b>\n{LINE}\n"
        "1️⃣ Tap <b>Open Key Page</b>\n"
        "2️⃣ Complete the steps on the page\n"
        "3️⃣ Your key appears — tap to copy 🎉\n"
        f"{LINE}\n"
        "⏱ <i>Valid 24 hours • 1 device • 1 key per 24 hours</i>\n"
        "🔒 <i>This link is only for you — open it within 20 minutes</i>"
        + note
    )
    inline = {"inline_keyboard": [[{"text": "🚀 Open Key Page", "url": link}]]}
    animated_send(cid, ["🔐 <b>Preparing</b> ▫️▫️▫️", "🔐 <b>Preparing</b> ◾▫️▫️", "🔐 <b>Preparing</b> ◾◾▫️", "🔐 <b>Ready</b> ◾◾◾"],
                  final, inline)


def show_settings(cid):
    c = db_get("config") or {}

    def v(x):
        return f"<code>{esc(x)}</code>" if x else "❌ <i>not set</i>"
    send(cid,
         f"⚙️ <b>Current Settings</b>\n{LINE}\n"
         f"📦 <b>APK Link:</b> {v(c.get('apk_link'))}\n\n"
         f"🎥 <b>Video Link:</b> {v(c.get('video_link'))}\n\n"
         f"🔗 <b>Key page:</b> <code>{esc(BASE_URL)}</code>")


def handle_message(msg):
    chat = msg.get("chat", {})
    if chat.get("type") != "private":
        return
    cid = chat["id"]
    text = (msg.get("text") or "").strip()
    is_admin = cid in ADMIN_CHAT_IDS

    if text.startswith("/start"):
        if is_admin:
            db_del(f"state/{cid}")
        welcome(cid, is_admin)
        return

    if text.startswith("/cancel"):
        if is_admin:
            db_del(f"state/{cid}")
        send(cid, "❎ <b>Cancelled</b>", menu_markup(is_admin))
        return

    # Admin is sending a link for a pending setting
    state = db_get(f"state/{cid}") if is_admin else None
    if is_admin and state and text not in ALL_BUTTONS:
        if not re.match(r"^https?://\S+$", text):
            send(cid, "⚠️ <b>Invalid link.</b>\nSend a full URL starting with <code>https://</code>\nor /cancel")
            return
        db_put(f"config/{state['field']}", text)
        db_del(f"state/{cid}")
        send(cid, f"✅ <b>{esc(state['label'])} saved</b>\n<code>{esc(text)}</code>", menu_markup(True))
        return

    if text == B_ID:
        show_id(cid)
    elif text == B_KEY:
        get_free_key(cid)
    elif text == B_VIDEO:
        link_button(cid, (db_get("config/video_link") or ""),
                    "🎬 <b>Setup Video</b>", "▶️ Watch Video", "Setup video is not available yet")
    elif text == B_APK:
        link_button(cid, (db_get("config/apk_link") or ""),
                    "📥 <b>Download APK</b>", "⬇️ Download APK", "APK is not available yet")
    elif is_admin and text in SET_MAP:
        field, label = SET_MAP[text]
        db_put(f"state/{cid}", {"field": field, "label": label})
        send(cid, f"✏️ <b>Send the new {esc(label)}</b>\n<i>Example:</i> <code>https://t.me/...</code>\n\n❌ /cancel to stop")
    elif is_admin and text == B_SETTINGS:
        show_settings(cid)
    else:
        send(cid, "👇 <i>Please use the menu buttons</i>", menu_markup(is_admin))


def safe_handle(msg):
    try:
        handle_message(msg)
    except Exception:
        traceback.print_exc()
        try:
            send(msg["chat"]["id"], "⚠️ <b>Temporary error.</b>\n<i>Please try again in a moment.</i>")
        except Exception:
            pass


# ---------------------------------------------------------------------
# Website API
# ---------------------------------------------------------------------
def err(msg, code=400, **extra):
    return jsonify({"ok": False, "error": msg, **extra}), code


def key_view(info, new=False):
    """Response for a token that already has a key."""
    left = info["key_ts"] + COOLDOWN_HOURS * 3600 - time.time()
    if left <= 0:
        return jsonify({"ok": True, "status": "expired"})
    return jsonify({"ok": True, "status": "active", "key": info["key"],
                    "expiry_date": info["expiry"], "remaining": int(left), "new": new})


@app.route("/api/claim", methods=["POST"])
def api_claim():
    body = request.get_json(silent=True) or {}
    token = str(body.get("t") or "")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{10,40}", token):
        return err("This link is invalid. Open the bot and press 🔑 Get Free Key.", 403)
    tk = th(token)

    try:
        with _lock:  # no double key creation from parallel opens
            info = db_get(f"tokens/{tk}")
            if not info:
                return err("This link is invalid or has been cancelled. Open the bot and press 🔑 Get Free Key.", 403)

            # Already has a key -> always show the SAME key (until it expires)
            if info.get("key"):
                return key_view(info)

            cid = info["cid"]
            age = time.time() - info["created"]
            if age > TOKEN_TTL_SECONDS:
                db_del(f"tokens/{tk}")
                return err("This link has expired. Open the bot and press 🔑 Get Free Key again.", 403)

            if cid not in ADMIN_CHAT_IDS and age < MIN_AD_SECONDS:
                db_del(f"tokens/{tk}")
                db_del(f"pending/{cid}")
                return err("The link was opened too fast. Open the bot, press 🔑 Get Free Key and complete the page properly.", 403)

            left = cooldown_left(cid)
            if left:
                return err(f"You already got a key. Next key in {fmt_wait(left)}.", 429)

            key, expiry = create_license_key()
            now = time.time()
            info.update(key=key, key_ts=now, expiry=expiry)
            db_patch(f"tokens/{tk}", {"key": key, "key_ts": now, "expiry": expiry})
            db_put(f"generated/{cid}", {"key": key, "ts": now, "tk": tk})
            db_del(f"pending/{cid}")
    except Exception as e:
        print("CLAIM ERROR:", e)
        traceback.print_exc()
        return err("Server error while creating the key. Please reload in a moment.", 500)

    send(cid,
         f"🎉 <b>Your Free Key</b>\n{LINE}\n"
         f"🔑 <code>{key}</code>\n{LINE}\n"
         f"📅 <b>Expires:</b> {expiry}\n"
         "📱 <b>Devices:</b> 1\n"
         "👆 <i>Tap the key to copy</i>")
    return key_view(info, new=True)


# ---------------------------------------------------------------------
# Web + webhook routes
# ---------------------------------------------------------------------
@app.route("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/health")
def health():
    return "ok"   # for UptimeRobot (keeps the free Render service awake)


@app.route("/status")
def status():
    out = {}
    out["telegram"] = "ok" if tg("getMe").get("ok") else "ERROR (check BOT_TOKEN)"
    try:
        db_get("config")
        out["firebase_RF_Hub"] = "ok"
    except Exception as e:
        out["firebase_RF_Hub"] = "ERROR: " + str(e)[:120]
    try:
        fb("GET", "DPMods_Security/App_Status")
        out["firebase_keys"] = "ok"
    except Exception as e:
        out["firebase_keys"] = "ERROR: " + str(e)[:120]
    try:
        r = requests.get("https://vplink.in", timeout=8)
        out["vplink"] = f"reachable ({r.status_code})"
    except Exception as e:
        out["vplink"] = "ERROR: " + type(e).__name__
    return jsonify(out)


@app.route("/webhook/<secret>", methods=["POST"])
def webhook(secret):
    if secret != WEBHOOK_SECRET:
        return "forbidden", 403
    upd = request.get_json(silent=True) or {}
    uid = upd.get("update_id")
    if uid is not None:
        if uid in _seen_updates:      # Telegram retry -> ignore
            return "ok"
        _seen_updates.append(uid)
    if "message" in upd:
        # answer Telegram immediately, work in the background
        threading.Thread(target=safe_handle, args=(upd["message"],), daemon=True).start()
    return "ok"


@app.route("/setwebhook")
def set_webhook():
    url = f"{BASE_URL.rstrip('/')}/webhook/{WEBHOOK_SECRET}"
    r = tg("setWebhook", url=url, allowed_updates=["message"], drop_pending_updates=True)
    tg("setMyCommands", commands=[{"command": "start", "description": "Open menu"},
                                  {"command": "cancel", "description": "Cancel current action"}])
    return jsonify({"webhook_url": url, "telegram_response": r})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
