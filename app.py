import os, json, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, redirect, jsonify, abort
import requests
from urllib.parse import quote_plus

# ====== CONFIG (ENV) ======
IST = ZoneInfo("Asia/Kolkata")
app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({"ok": True, "msg": "Bot is live!"})

BOT_TOKEN = os.environ["BOT_TOKEN"]                # e.g. 123456:ABC...
CHANNEL_ID = os.environ["CHANNEL_ID"]              # e.g. -1001234567890  (numeric recommended)
BASE_URL = os.environ["BASE_URL"].rstrip("/")      # e.g. https://your-app.onrender.com
PRICE_INR = int(os.environ.get("PRICE_INR", "2500"))
SUBS_DAYS = int(os.environ.get("SUBSCRIPTION_DAYS", "30"))
INVITE_TTL = int(os.environ.get("INVITE_LINK_TTL_SECONDS", "600"))  # 600 = 10 minutes
CRON_SECRET = os.environ.get("CRON_SECRET", "")    # optional header secret

# Instamojo
IM_API_BASE = "https://www.instamojo.com/api/1.1"
IM_BEARER = os.environ.get("INSTAMOJO_AUTH_TOKEN", "").strip()  # recommended
IM_KEY = os.environ.get("INSTAMOJO_API_KEY", "").strip()        # legacy
IM_TOKEN = os.environ.get("INSTAMOJO_API_TOKEN", "").strip()    # legacy

def im_headers():
    if IM_BEARER:
        return {"Authorization": f"Bearer {IM_BEARER}", "Content-Type": "application/x-www-form-urlencoded"}
    return {"X-Api-Key": IM_KEY, "X-Auth-Token": IM_TOKEN, "Content-Type": "application/x-www-form-urlencoded"}

# ====== LIGHT DB (JSON FILE) ======
DATA_DIR = "data"
DB_FILE = os.path.join(DATA_DIR, "subscribers.json")
os.makedirs(DATA_DIR, exist_ok=True)

def load_db():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_db(d):
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_FILE)

DB = load_db()  # { "<tg_id>": {"expiry_ts": int, "last_payment": iso, "status": "active|expired"} }

# ====== TELEGRAM API HELPERS ======
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def tg_send_message(chat_id: int, text: str, parse_mode=None, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode: payload["parse_mode"] = parse_mode
    if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(f"{TG_API}/sendMessage", json=payload, timeout=10)
    except Exception:
        pass

def tg_create_invite_link(expire_seconds: int, member_limit: int = 1) -> str:
    expire_unix = int(time.time()) + max(60, int(expire_seconds))
    payload = {"chat_id": CHANNEL_ID, "expire_date": expire_unix, "member_limit": member_limit}
    r = requests.post(f"{TG_API}/createChatInviteLink", json=payload, timeout=10)
    j = r.json() if r.ok else {}
    return j.get("result", {}).get("invite_link")

def tg_remove_user(user_id: int):
    # ban+unban = remove
    try:
        requests.post(f"{TG_API}/banChatMember", json={"chat_id": CHANNEL_ID, "user_id": user_id}, timeout=10)
    except Exception:
        pass
    try:
        requests.post(f"{TG_API}/unbanChatMember", json={"chat_id": CHANNEL_ID, "user_id": user_id, "only_if_banned": True}, timeout=10)
    except Exception:
        pass

# ====== FLASK APP ======
app = Flask(__name__)

@app.get("/")
def health():
    return {"ok": True, "time": datetime.now(IST).isoformat()}

# Telegram webhook (set to BASE_URL + /telegram-webhook)
@app.post("/telegram-webhook")
def telegram_webhook():
    data = request.get_json(force=True)
    msg = data.get("message") or data.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    uid = chat.get("id")
    if not uid:
        return jsonify({"ok": True})

    if text.startswith("/start"):
        welcome = (
            "🙏 *Welcome!*\n\n"
            "एक सही फैसला आपकी दिशा बदल सकता है.\n"
            "हमारी *premium community* में curated insights, discipline और guidance—\n"
            "ताकि अगले 30 दिनों में आप बेहतर decisions ले सकें.\n\n"
            f"💰 *Fee:* ₹{PRICE_INR}/month\n"
            "👇 सुरक्षित भुगतान करें और तुरंत जुड़ें:"
        )
        pay_url = f"{BASE_URL}/pay?tg={uid}"
        keyboard = {"inline_keyboard": [[{"text": f"💳 Pay ₹{PRICE_INR} & Join", "url": pay_url}]]}
        tg_send_message(uid, welcome, parse_mode="Markdown", reply_markup=keyboard)

    return jsonify({"ok": True})

# Create Instamojo payment request & redirect to checkout
@app.get("/pay")
def pay():
    tg = (request.args.get("tg") or "").strip()
    if not tg.isdigit():
        return "Invalid request", 400
    payload = {
        "purpose": "Premium Membership",
        "amount": str(PRICE_INR),
        "redirect_url": f"{BASE_URL}/payment-return",
        "webhook": f"{BASE_URL}/instamojo-webhook",
        "allow_repeated_payments": "false",
        "metadata": json.dumps({"telegram_user_id": tg})
    }
    body = "&".join([f"{k}={quote_plus(v)}" for k, v in payload.items()])
    r = requests.post(f"{IM_API_BASE}/payment-requests/", data=body, headers=im_headers(), timeout=20)
    if not r.ok:
        return f"Payment creation failed: {r.text}", 500
    pr = r.json().get("payment_request", {})
    return redirect(pr.get("longurl"), code=302)

@app.get("/payment-return")
def payment_return():
    return "<h3>Thanks! Payment received (if successful). Check your Telegram for the invite link.</h3>"

# Instamojo webhook → verify → invite + save expiry
@app.post("/instamojo-webhook")
def instamojo_webhook():
    form = request.form.to_dict()
    req_id = form.get("payment_request_id") or form.get("payment_request") or ""
    if not req_id:
        return "no id", 200

    try:
        vr = requests.get(f"{IM_API_BASE}/payment-requests/{req_id}/", headers=im_headers(), timeout=20)
        vr.raise_for_status()
        pr = vr.json().get("payment_request", {})
    except Exception:
        return "verify failed", 200

    status = pr.get("status", "")
    if status not in ("Completed", "Credit", "Success"):
        return "ignored", 200

    meta = pr.get("metadata") or {}
    if isinstance(meta, str):
        try: meta = json.loads(meta)
        except Exception: meta = {}
    tg = str(meta.get("telegram_user_id", "")).strip()
    if not tg.isdigit():
        return "no user", 200

    # success → send invite + save
    try:
        invite = tg_create_invite_link(INVITE_TTL, member_limit=1)
        expiry_dt = datetime.now(IST) + timedelta(days=SUBS_DAYS)
        DB[tg] = {"expiry_ts": int(expiry_dt.timestamp()), "last_payment": datetime.now(IST).isoformat(), "status": "active"}
        save_db(DB)
        msg = (f"✅ *Payment Successful!*\n\n"
               f"यह आपकी *private invite link* है (1 बार valid, {INVITE_TTL//60} मिनट में expire):\n"
               f"{invite}\n\n_Validity: {SUBS_DAYS} days._")
        tg_send_message(int(tg), msg, parse_mode="Markdown")
    except Exception:
        pass

    return "ok", 200

# Daily (or manual) expiry check
@app.get("/run-expiry")
def run_expiry():
    if CRON_SECRET and request.headers.get("X-CRON-SECRET") != CRON_SECRET:
        abort(401)
    now_ts = int(datetime.now(IST).timestamp())
    expired = 0
    for uid, rec in list(DB.items()):
        if rec.get("status") == "active" and int(rec.get("expiry_ts", 0)) <= now_ts:
            try:
                tg_remove_user(int(uid))
            except Exception:
                pass
            DB[uid]["status"] = "expired"
            DB[uid]["expired_at"] = datetime.now(IST).isoformat()
            try:
                tg_send_message(int(uid), f"🚫 आपकी subscription खत्म हो गई है.\nदोबारा जुड़ने के लिए पेमेंट करें:\n{BASE_URL}/pay?tg={uid}")
            except Exception:
                pass
            expired += 1
    if expired:
        save_db(DB)
    return jsonify({"expired": expired})

# Local dev only
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
