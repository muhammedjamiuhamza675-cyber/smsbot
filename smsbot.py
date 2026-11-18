"""
textsms_bot.py — Telegram bot for textsms.io virtual number rentals.
Raw Bot API + infinite polling loop. No Application/Updater framework.
Railway-ready. Naira only. Admin-approved deposits. Auto-finish on code.

Referral (single system):
    • Inviter gets ₦200 + ₦100 = ₦300 when a referred user makes their
      FIRST purchase (only).
    • Bonus is credited to the inviter's referral pool, spendable on
      numbers OR withdrawable via admin manual payout.

Deposit flow: 3 steps (account shown first, then amount, then screenshot).

Admin extras:
    • Social links editor (TG channel/group, WA group/channel)
    • Complaint inbox with reply routing (in-bot or /reply)
    • Broadcast with text + photo + video
    • /paid and /withdrawfail for manual payout release
"""

from __future__ import annotations

import asyncio
import copy
import html
import json
import logging
import os
import secrets
import threading
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("BOT_DATA_DIR", BASE_DIR)
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = BASE_DIR

if os.environ.get("RAILWAY_ENVIRONMENT") and DATA_DIR == BASE_DIR:
    logging.warning(
        "RAILWAY detected but BOT_DATA_DIR not set to a volume path. "
        "State will be WIPED on redeploy. Set BOT_DATA_DIR=/data and mount "
        "a volume at /data.")

STATE_PATH = os.path.join(DATA_DIR, ".textsms_state.json")

BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "8857671587:AAEsQskQKWF4ge8hsUa94i3LhVh3zJ_7SCc",
).strip()
OWNER_ID = int(os.environ.get("BOT_OWNER_ID", "7443685686"))
BOT_DISPLAY_NAME = "Crackers"
ADMIN_USERNAME = os.environ.get("BOT_ADMIN_USERNAME", "").lstrip("@")

TEXTSMS_BASE = "https://textsms.io/api/v1"
TEXTSMS_KEY = os.environ.get("TEXTSMS_KEY", "")

POLL_INTERVAL = 8
TG_LONGPOLL_TIMEOUT = 25
TG_RETRY_BACKOFF = 3
MAX_ACTIVE_PER_USER = 3
QUEUE_RETRY_SECONDS = 30
QUEUE_RETRY_FAST_SECONDS = 15
QUEUE_MAX_MINUTES = 30
CATALOGUE_CACHE_SECONDS = 600
BALANCE_WATCH_SECONDS = 60
ADMIN_ALERT_COOLDOWN_SECONDS = 300

WELCOME_TEXT = (
    "🔥 Welcome to {name} — the strongest SMS verification bot.\n\n"
    "Tired of numbers that expire before your code arrives? "
    "Tired of apps rejecting your virtual numbers?\n\n"
    "We fix that.\n\n"
    "✅ Long-lasting WhatsApp accounts — numbers stay active through "
    "the full verification window, plus Telegram, Instagram, TikTok, "
    "Facebook, Google, and hundreds more\n"
    "✅ Instant delivery — number arrives in seconds\n"
    "✅ Auto code watching — we send the code the moment it arrives\n"
    "✅ Auto-refund — no code? Money back automatically\n"
    "✅ 24/7 — works while you sleep\n\n"
    "💰 Your balance: {balance}\n\n"
    "🚀 Tap a button below to buy your first number now.\n\n"
    "Why waste money on numbers that don't work? "
    "Start with us today — thousands of codes delivered."
)

HELP_TEXT = (
    "ℹ️ <b>How to use this bot</b>\n\n"
    "<b>Step 1 — Add money</b>\n"
    "Tap 💳 Add Money → transfer to the account shown → type the amount "
    "you sent → tap ✅ I have paid → send a screenshot of your bank "
    "receipt → wait for admin approval.\n\n"
    "<b>Step 2 — Buy a number</b>\n"
    "Tap 📱 Buy. Browse the services and tap the one you need.\n\n"
    "<b>Step 3 — Get your code</b>\n"
    "Enter the number in the app you're verifying. We watch the number "
    "24/7. The moment the code arrives, we send it here automatically.\n\n"
    "<b>Step 4 — Refunds</b>\n"
    "No code? Full refund, automatic. You can also cancel manually "
    "after 2 minutes.\n\n"
    "<b>🎁 Referral program</b>\n"
    "Invite friends. When a friend buys their first number, you earn "
    "<b>₦300</b> instantly. Spend it on numbers or withdraw it to your "
    "bank — your choice.\n\n"
    "<b>Bottom menu</b>\n"
    "📱 Buy · 💰 Balance · 💳 Add Money · 📜 History\n"
    "🔄 Active · 🌐 Channels · 🎁 Referral · 🆘 Support · ℹ️ Help\n\n"
    "Questions? Tap 🆘 Support or message the admin directly."
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("textsms-bot")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

_SAVE_LOCK = threading.Lock()

def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with _SAVE_LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)

def shorten(text: Any, limit: int = 30) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: max(1, limit - 1)].rstrip() + "…"

def fmt_ngn(value: Any) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "₦0"
    return f"₦{n:,.0f}" if n.is_integer() else f"₦{n:,.2f}"

def ts_str(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(ts)))
    except Exception:
        return "—"

def admin_link(st: dict) -> str | None:
    s = settings(st)
    handle = (s.get("admin_username") or ADMIN_USERNAME or "").lstrip("@")
    return f"https://t.me/{handle}" if handle else None

# ---------------------------------------------------------------------------
# textsms.io client
# ---------------------------------------------------------------------------

class TextSMSError(Exception):
    def __init__(self, code: str, message: str, status: int = 0):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status

class TextSMS:
    def __init__(self, key: str, base: str = TEXTSMS_BASE) -> None:
        self.key = key
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "textsms-tg-bot/1.0",
        })

    def _call(self, method: str, path: str, *, body: dict | None = None,
              params: dict | None = None,
              idempotency_key: str | None = None) -> dict:
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key[:120]
        try:
            r = self.session.request(method, self.base + path, json=body,
                                     params=params, headers=headers, timeout=30)
        except requests.RequestException as e:
            raise TextSMSError("network", str(e), 0) from e
        try:
            data = r.json()
        except Exception:
            raise TextSMSError("bad_response", r.text[:200], r.status_code)
        if not data.get("ok"):
            err = data.get("error") or {}
            raise TextSMSError(
                str(err.get("code") or "unknown"),
                str(err.get("message") or "no message"),
                r.status_code)
        return data

    def services(self) -> dict:
        return self._call("GET", "/services")

    def balance(self) -> dict:
        return self._call("GET", "/balance")

    def rent(self, service_id: int, idempotency_key: str) -> dict:
        return self._call("POST", "/rent", body={"service_id": service_id},
                          idempotency_key=idempotency_key)

    def status(self, rental_id: int) -> dict:
        return self._call("GET", "/status", params={"id": rental_id})

    def cancel(self, rental_id: int) -> dict:
        return self._call("POST", "/cancel", body={"id": rental_id})

    def finish(self, rental_id: int) -> dict:
        return self._call("POST", "/finish", body={"id": rental_id})

# ---------------------------------------------------------------------------
# Telegram raw bot
# ---------------------------------------------------------------------------

class TelegramBot:
    def __init__(self, token: str) -> None:
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        self.offset = 0
        self._me: dict | None = None
        self._handlers: list[tuple[callable, callable]] = []

    async def _call(self, method: str, **params) -> dict:
        try:
            r = await self.client.post(f"{self.base}/{method}", json=params)
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"telegram {method} failed: {e}") from e
        if not data.get("ok"):
            raise RuntimeError(
                f"telegram {method} error: {data.get('description')}")
        return data.get("result") or {}

    async def get_me(self) -> dict:
        if self._me is None:
            self._me = await self._call("getMe")
        return self._me

    @property
    def username(self) -> str:
        return (self._me or {}).get("username") or "bot"

    async def send_message(self, chat_id: int, text: str, **kw) -> dict:
        params = {"chat_id": chat_id, "text": text}
        for k, v in kw.items():
            if v is not None:
                if isinstance(v, (dict, list)):
                    params[k] = json.dumps(v)
                else:
                    params[k] = v
        return await self._call("sendMessage", **params)

    async def send_photo(self, chat_id: int, photo: str, caption: str = "",
                         **kw) -> dict:
        params = {"chat_id": chat_id, "photo": photo, "caption": caption}
        for k, v in kw.items():
            if v is not None:
                if isinstance(v, (dict, list)):
                    params[k] = json.dumps(v)
                else:
                    params[k] = v
        return await self._call("sendPhoto", **params)

    async def send_video(self, chat_id: int, video: str, caption: str = "",
                         **kw) -> dict:
        params = {"chat_id": chat_id, "video": video, "caption": caption}
        for k, v in kw.items():
            if v is not None:
                if isinstance(v, (dict, list)):
                    params[k] = json.dumps(v)
                else:
                    params[k] = v
        return await self._call("sendVideo", **params)

    async def edit_message_text(self, chat_id: int, message_id: int,
                                 text: str, **kw) -> dict:
        params = {"chat_id": chat_id, "message_id": message_id, "text": text}
        for k, v in kw.items():
            if v is not None:
                if isinstance(v, (dict, list)):
                    params[k] = json.dumps(v)
                else:
                    params[k] = v
        return await self._call("editMessageText", **params)

    async def answer_callback_query(self, cb_id: str, text: str = "",
                                     **kw) -> dict:
        params = {"callback_query_id": cb_id}
        if text:
            params["text"] = text
        for k, v in kw.items():
            if v is not None:
                params[k] = v
        return await self._call("answerCallbackQuery", **params)

    async def set_my_name(self, name: str) -> dict:
        return await self._call("setMyName", name=name)

    async def set_my_commands(self, commands: list[dict]) -> dict:
        return await self._call("setMyCommands",
                                commands=json.dumps(commands))

    def add_handler(self, filter_fn, handler) -> None:
        self._handlers.append((filter_fn, handler))

    async def dispatch(self, update: dict) -> None:
        for filter_fn, handler in self._handlers:
            try:
                if filter_fn(update):
                    await handler(update)
                    return
            except Exception as e:
                log.exception("handler error: %s", e)

    async def poll_forever(self) -> None:
        backoff = TG_RETRY_BACKOFF
        while True:
            try:
                params = {
                    "offset": self.offset,
                    "timeout": TG_LONGPOLL_TIMEOUT,
                    "allowed_updates": json.dumps([
                        "message", "callback_query", "pre_checkout_query",
                    ]),
                }
                r = await self.client.post(
                    f"{self.base}/getUpdates", json=params,
                    timeout=TG_LONGPOLL_TIMEOUT + 15)
                data = r.json()
                if not data.get("ok"):
                    log.warning("getUpdates not ok: %s",
                                data.get("description"))
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                backoff = TG_RETRY_BACKOFF
                for update in data.get("result") or []:
                    self.offset = max(self.offset,
                                      int(update["update_id"]) + 1)
                    try:
                        await self.dispatch(update)
                    except Exception as e:
                        log.exception("dispatch error: %s", e)
            except (httpx.ReadTimeout, httpx.ConnectTimeout,
                    httpx.RemoteProtocolError):
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("poll loop error: %s — retrying in %ss",
                            e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def close(self) -> None:
        try:
            await self.client.aclose()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def new_user(uid: int, name: str = "") -> dict:
    return {
        "uid": uid,
        "name": name,
        "created": time.time(),
        "balance": 0.0,
        "active_rentals": {},
        "history": [],
        "deposits": [],
        "pending_rentals": [],
        "payments": [],
        "rate_window": [],
        "banned": False,
        "successful_rentals": 0,
        "referral": {},
        "invite": {},
        "started": False,
    }

SETTINGS_DEFAULT = {
    "owner_id": OWNER_ID,
    "admin_username": ADMIN_USERNAME,
    "api_key": TEXTSMS_KEY,
    "maintenance": False,
    "max_per_minute": 10,
    "safety_buffer_ngn": 200,
    "balance_cache_ttl": 30,
    "service_prices": {},
    "bank": {
        "enabled": True,
        "bank_name": "PayRep MFB",
        "account_number": "9167685658",
        "account_name": "HAMZA JAMIU MUHAMMED",
        "min_deposit_ngn": 1000,
    },
    "referral": {
        "enabled": False,
        "bonus_first_ngn": 200,
        "bonus_extra_ngn": 100,
        "min_withdraw_ngn": 500,
    },
    "socials": {
        "telegram_channel": "",
        "telegram_group": "",
        "whatsapp_group": "",
        "whatsapp_channel": "",
    },
    "admin_chat_state": {},
    "admin_low_wallet_alert_ts": 0.0,
    "complaints": {},
    "complaint_counter": 1000,
}

_STATE: dict | None = None
_STATE_LOCK = threading.RLock()

def load_state() -> dict:
    global _STATE
    if _STATE is None:
        with _STATE_LOCK:
            if _STATE is None:
                st = load_json(STATE_PATH, None)
                if not isinstance(st, dict):
                    st = {"users": {}}
                st.setdefault("users", {})
                s = st.setdefault("settings", {})
                for k, v in SETTINGS_DEFAULT.items():
                    if isinstance(v, dict) and isinstance(s.get(k), dict):
                        for kk, vv in v.items():
                            s[k].setdefault(kk, copy.deepcopy(vv))
                    else:
                        s.setdefault(k, copy.deepcopy(v))
                _STATE = st
    return _STATE

def mutate_state(fn):
    global _STATE
    with _STATE_LOCK:
        st = load_state()
        result = fn(st)
        snapshot = copy.deepcopy(st)
        save_json(STATE_PATH, snapshot)
        return result

def save_state() -> None:
    if _STATE is None:
        return
    with _STATE_LOCK:
        snapshot = copy.deepcopy(_STATE)
        save_json(STATE_PATH, snapshot)

def get_user(st: dict, uid: int, name: str = "") -> dict:
    u = st["users"].get(str(uid))
    if not isinstance(u, dict):
        u = new_user(uid, name)
        st["users"][str(uid)] = u
    if name and not u.get("name"):
        u["name"] = name
    for key, factory in (("active_rentals", dict), ("history", list),
                         ("deposits", list), ("pending_rentals", list),
                         ("payments", list), ("rate_window", list),
                         ("referral", dict), ("invite", dict)):
        if not isinstance(u.get(key), factory):
            u[key] = factory()
    return u

def settings(st: dict) -> dict:
    return st.setdefault("settings", {})

def is_owner(uid: int, st: dict | None = None) -> bool:
    if st is None:
        st = load_state()
    oid = int(settings(st).get("owner_id") or 0)
    return bool(oid) and uid == oid

def gateway(st: dict) -> TextSMS:
    key = str(settings(st).get("api_key") or "")
    if not key:
        raise TextSMSError("unauthenticated", "API key not set", 401)
    return TextSMS(key)

# ---------------------------------------------------------------------------
# Wallet: spendable = balance + referral pool
# ---------------------------------------------------------------------------

def balance(u: dict) -> float:
    return float(u.get("balance") or 0.0)

def referral_available(u: dict) -> float:
    r = u.get("referral") or {}
    earned = float(r.get("earnings_ngn") or 0)
    spent = float(r.get("spent_on_numbers_ngn") or 0)
    paid = float(r.get("paid_out_ngn") or 0)
    reserved = float(r.get("reserved_ngn") or 0)
    return max(0.0, earned - spent - paid - reserved)

def total_spendable(u: dict) -> float:
    return balance(u) + referral_available(u)

def add_balance(u: dict, n: float) -> None:
    with _STATE_LOCK:
        u["balance"] = max(0.0, balance(u) + float(n))

def charge_balance(u: dict, n: float) -> dict:
    """Charge spendable total: drain real balance first, then referral pool.
    Returns {'from_balance': x, 'from_referral': y}."""
    n = float(n)
    with _STATE_LOCK:
        from_bal = min(balance(u), n)
        u["balance"] = max(0.0, balance(u) - from_bal)
        remaining = n - from_bal
        from_ref = 0.0
        if remaining > 0:
            avail = referral_available(u)
            from_ref = min(avail, remaining)
            r = u.setdefault("referral", {})
            r["spent_on_numbers_ngn"] = (
                float(r.get("spent_on_numbers_ngn") or 0) + from_ref)
        return {"from_balance": from_bal, "from_referral": from_ref}

def refund_balance(u: dict, n: float, source: dict | None = None) -> None:
    """Refund a charge. If source dict is provided, put money back where it
    came from; otherwise, refund to real balance."""
    n = float(n)
    if not n:
        return
    if source:
        back_ref = float(source.get("from_referral") or 0)
        back_bal = float(source.get("from_balance") or 0)
        if back_bal > 0:
            add_balance(u, back_bal)
        if back_ref > 0:
            with _STATE_LOCK:
                r = u.setdefault("referral", {})
                r["spent_on_numbers_ngn"] = max(
                    0.0, float(r.get("spent_on_numbers_ngn") or 0) - back_ref)
        return
    add_balance(u, n)

def rate_ok(u: dict, per_minute: int) -> bool:
    now = time.time()
    window = [t for t in (u.get("rate_window") or []) if now - t < 60]
    if len(window) >= per_minute:
        return False
    window.append(now)
    u["rate_window"] = window
    return True

def price_in_ngn(st: dict, service_id: int | None) -> float | None:
    if service_id is None:
        return None
    prices = settings(st).get("service_prices") or {}
    raw = prices.get(str(service_id))
    if raw is None:
        return None
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_BALANCE_CACHE = {"ngn": 0.0, "ts": 0.0}
_CATALOGUE_CACHE: dict[str, Any] = {"ts": 0.0, "services": []}

async def get_provider_balance(st: dict, force: bool = False) -> float:
    s = settings(st)
    ttl = float(s.get("balance_cache_ttl") or 30)
    now = time.time()
    if not force and now - _BALANCE_CACHE["ts"] < ttl:
        return float(_BALANCE_CACHE["ngn"] or 0)
    try:
        resp = await asyncio.to_thread(lambda: gateway(st).balance())
        _BALANCE_CACHE["ngn"] = float(resp.get("balance") or 0)
        _BALANCE_CACHE["ts"] = now
        return _BALANCE_CACHE["ngn"]
    except TextSMSError as e:
        log.warning("balance check failed: %s", e)
        return float(_BALANCE_CACHE["ngn"] or 0)

async def fetch_services_cached(st: dict, force: bool = False) -> list[dict]:
    now = time.time()
    if (not force and _CATALOGUE_CACHE["services"]
            and now - _CATALOGUE_CACHE["ts"] < CATALOGUE_CACHE_SECONDS):
        return _CATALOGUE_CACHE["services"]
    try:
        resp = await asyncio.to_thread(lambda: gateway(st).services())
        services = resp.get("services") or []
        _CATALOGUE_CACHE["services"] = services
        _CATALOGUE_CACHE["ts"] = now
        return services
    except TextSMSError as e:
        log.warning("catalogue fetch: %s", e)
        return _CATALOGUE_CACHE["services"]

# ---------------------------------------------------------------------------
# Referral — ₦300 on referred user's FIRST purchase only
# ---------------------------------------------------------------------------

def ensure_referral(u: dict) -> dict:
    r = u.setdefault("referral", {})
    if not r.get("code"):
        r["code"] = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8].upper()
    r.setdefault("referred_by", None)
    r.setdefault("earnings_ngn", 0.0)
    r.setdefault("spent_on_numbers_ngn", 0.0)
    r.setdefault("paid_out_ngn", 0.0)
    r.setdefault("reserved_ngn", 0.0)
    r.setdefault("events", [])
    r.setdefault("milestones_hit", [])
    return r

def find_referral_owner(st: dict, code: str) -> int | None:
    code = (code or "").strip().upper()
    for uid_str, u in st.get("users", {}).items():
        if (u.get("referral") or {}).get("code", "").upper() == code:
            try:
                return int(uid_str)
            except ValueError:
                return None
    return None

def apply_referral_bonus(st: dict, buyer_uid: int, rental_id: int) -> dict | None:
    """Credit the inviter ₦200 + ₦100 = ₦300 on the referred user's FIRST
    purchase only. No second milestone."""
    buyer = get_user(st, buyer_uid)
    r = ensure_referral(buyer)
    inviter_uid = r.get("referred_by")
    if not inviter_uid:
        return None
    cfg = settings(st).get("referral") or {}
    if not cfg.get("enabled"):
        return None
    done = int(buyer.get("successful_rentals") or 0)
    if done != 1:
        return None
    if "first_purchase" in r["milestones_hit"]:
        return None
    base = float(cfg.get("bonus_first_ngn") or 200)
    extra = float(cfg.get("bonus_extra_ngn") or 100)
    total = base + extra
    try:
        inviter = get_user(st, int(inviter_uid))
    except (TypeError, ValueError):
        return None
    ir = ensure_referral(inviter)
    ir["earnings_ngn"] = float(ir.get("earnings_ngn") or 0) + total
    ir["events"].append({
        "ts": time.time(), "kind": "referral_bonus",
        "amount_ngn": total, "base_ngn": base, "extra_ngn": extra,
        "from_uid": buyer_uid, "rental_id": rental_id,
    })
    r["milestones_hit"].append("first_purchase")
    return {"inviter_uid": int(inviter_uid), "amount": total,
            "base": base, "extra": extra,
            "inviter_available": referral_available(inviter)}

# ---------------------------------------------------------------------------
# Complaints
# ---------------------------------------------------------------------------

def next_complaint_id(st: dict) -> int:
    s = settings(st)
    n = int(s.get("complaint_counter") or 1000) + 1
    s["complaint_counter"] = n
    return n

def save_complaint(st: dict, ticket: int, user_uid: int, user_name: str,
                    text: str, admin_msg_id: int) -> None:
    s = settings(st)
    comps = s.setdefault("complaints", {})
    comps[str(ticket)] = {
        "ticket": ticket,
        "uid": user_uid,
        "name": user_name,
        "text": text,
        "admin_msg_id": admin_msg_id,
        "created_ts": time.time(),
        "status": "open",
        "replies": [],
    }

# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def ibtn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}

def inline_kb(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}

def main_menu(admin: bool = False, u: dict | None = None) -> dict:
    rows = [
        [ibtn("📱 Buy a number", "menu:services")],
        [ibtn("🔄 My numbers", "menu:active"),
         ibtn("📜 History", "menu:history")],
        [ibtn("💳 Add money", "menu:topup"),
         ibtn("💰 My balance", "menu:balance")],
        [ibtn("🌐 Channels", "menu:channels"),
         ibtn("🎁 Referral", "menu:referral")],
        [ibtn("🆘 Support", "menu:support"),
         ibtn("ℹ️ How it works", "menu:help")],
    ]
    if u and (u.get("pending_rentals") or []):
        n = len(u["pending_rentals"])
        rows.insert(1, [ibtn(f"⏳ Processing ({n})", "menu:pending")])
    if admin:
        rows.append([ibtn("👑 Admin panel", "admin:panel")])
    return inline_kb(rows)

def back_menu() -> dict:
    return inline_kb([[ibtn("⬅️ Main menu", "menu:main")]])

def reply_keyboard(admin: bool = False) -> dict:
    rows = [
        [{"text": "📱 Buy"}, {"text": "💰 Balance"}],
        [{"text": "💳 Add Money"}, {"text": "📜 History"}],
        [{"text": "🔄 Active"}, {"text": "🌐 Channels"}],
        [{"text": "🎁 Referral"}, {"text": "🆘 Support"}],
        [{"text": "ℹ️ Help"}],
    ]
    if admin:
        rows.append([{"text": "👑 Admin"}])
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Tap a button below…",
    }

def admin_menu(st: dict) -> dict:
    s = settings(st)
    key_set = "✅ SET" if s.get("api_key") else "❌ MISSING"
    leg_on = "🟢 ON" if (s.get("referral") or {}).get("enabled") else "🔴 OFF"
    maint = "🟠 ON" if s.get("maintenance") else "🟢 OFF"
    n_dep = sum(
        1 for u in st.get("users", {}).values()
        for d in (u.get("deposits") or []) if d.get("status") == "pending"
    )
    n_comp = sum(1 for c in (s.get("complaints") or {}).values()
                 if c.get("status") == "open")
    dep_label = f"📥 Pending Deposits ({n_dep})" if n_dep else "📥 Pending Deposits"
    comp_label = f"✍️ Complaints ({n_comp})" if n_comp else "✍️ Complaints"

    return inline_kb([
        [ibtn("🔑 API Key", "admin:set_key"),
         ibtn("💰 Provider Wallet", "admin:balance")],
        [ibtn("💲 Set Prices", "admin:prices:0"),
         ibtn("🔍 Search & Price", "admin:search_price")],
        [ibtn("⚡ Bulk Prices", "admin:bulk_prices")],
        [ibtn(dep_label, "admin:deposits")],
        [ibtn("➕ Add Balance", "admin:addbal"),
         ibtn("➖ Deduct Balance", "admin:deduct")],
        [ibtn("🚫 Ban / Unban User", "admin:ban")],
        [ibtn("🌐 Social Links", "admin:socials"),
         ibtn("📞 Set Admin Username", "admin:set_admin")],
        [ibtn(comp_label, "admin:complaints")],
        [ibtn(f"🎯 Referral: {leg_on}", "admin:toggle_legacy")],
        [ibtn("📢 Broadcast", "admin:broadcast"),
         ibtn("📊 Live Stats", "admin:stats")],
        [ibtn(f"🚧 Maintenance: {maint}", "admin:toggle_maint")],
        [ibtn("⬅️ Main menu", "menu:main")],
    ])

def welcome_text(u: dict, admin: bool) -> str:
    name = esc(u.get("name") or "friend")
    return WELCOME_TEXT.format(name=name, balance=fmt_ngn(balance(u)))

def thank_you_card(u: dict) -> str:
    return (
        "🎉 <b>Thank you for your purchase!</b>\n\n"
        f"💰 Your spendable balance: <b>{fmt_ngn(total_spendable(u))}</b>\n\n"
        "🚀 More features coming soon — stay tuned!\n\n"
        "🆘 Need help? Tap <b>Support</b> below or message the admin."
    )

def channels_card(st: dict) -> tuple[str, dict]:
    soc = settings(st).get("socials") or {}
    lines = ["🌐 <b>Our Channels & Groups</b>\n",
             "Join any of these to stay updated:\n"]
    rows = []
    tg_ch = (soc.get("telegram_channel") or "").strip()
    tg_gr = (soc.get("telegram_group") or "").strip()
    wa_gr = (soc.get("whatsapp_group") or "").strip()
    wa_ch = (soc.get("whatsapp_channel") or "").strip()
    if tg_ch:
        rows.append([{"text": "📢 Telegram Channel", "url": tg_ch}])
    if tg_gr:
        rows.append([{"text": "💬 Telegram Group", "url": tg_gr}])
    if wa_gr:
        rows.append([{"text": "🟢 WhatsApp Group", "url": wa_gr}])
    if wa_ch:
        rows.append([{"text": "🟩 WhatsApp Channel", "url": wa_ch}])
    if not rows:
        lines = ["🌐 <b>Our Channels</b>\n\n"
                 "No channels added yet. Check back soon!"]
    rows.append([ibtn("⬅️ Main menu", "menu:main")])
    return "\n".join(lines), inline_kb(rows)

def support_card(st: dict) -> tuple[str, dict]:
    link = admin_link(st)
    text = (
        "🆘 <b>Support</b>\n\n"
        "💬 <b>Message the admin directly</b> — for urgent questions.\n\n"
        "✍️ <b>Send a complaint</b> — describe your problem, get a "
        "ticket number, and the admin will reply here.\n\n"
        "⏱ Typical reply: within a few minutes."
    )
    rows = []
    if link:
        rows.append([{"text": "💬 Message Admin", "url": link}])
    rows.append([ibtn("✍️ Send a Complaint", "support:complain")])
    rows.append([ibtn("⬅️ Main menu", "menu:main")])
    return text, inline_kb(rows)

# ---------------------------------------------------------------------------
# Per-user session
# ---------------------------------------------------------------------------

_SESSIONS: dict[int, dict] = {}

def session(uid: int) -> dict:
    s = _SESSIONS.get(uid)
    if s is None:
        s = {"state": None, "admin_state": None, "svc_country": None,
             "svc_query": "", "price_sid": None, "bulk": None,
             "dep_amount": None, "socials_key": None,
             "withdraw_amount": None, "broadcast_payload": {}}
        _SESSIONS[uid] = s
    return s

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_start(update: dict) -> None:
    m = update["message"]
    uid = m["from"]["id"]
    st = load_state()
    u = get_user(st, uid, m["from"].get("first_name") or "")
    ses = session(uid)
    for k in list(ses.keys()):
        ses[k] = None if k not in ("svc_query", "broadcast_payload") else (
            "" if k == "svc_query" else {})
    admin = is_owner(uid, st)

    if u.get("banned") and not admin:
        await bot.send_message(
            m["chat"]["id"],
            "⛔ <b>Your account has been suspended.</b>",
            parse_mode="HTML")
        return

    text = m.get("text") or ""
    parts = text.split()
    if len(parts) >= 2 and parts[1].startswith("ref_"):
        code = parts[1][4:]
        inviter = find_referral_owner(st, code)
        if inviter and inviter != uid:
            r = ensure_referral(u)
            if not r.get("referred_by"):
                r["referred_by"] = str(inviter)
                save_state()

    if not u.get("started"):
        u["started"] = True
    save_state()

    await bot.send_message(
        m["chat"]["id"], welcome_text(u, admin),
        parse_mode="HTML", reply_markup=reply_keyboard(admin))

async def cmd_help(update: dict) -> None:
    m = update["message"]
    st = load_state()
    admin = is_owner(m["from"]["id"], st)
    await bot.send_message(m["chat"]["id"], HELP_TEXT, parse_mode="HTML",
                           reply_markup=reply_keyboard(admin))

async def cmd_cancel(update: dict) -> None:
    m = update["message"]
    st = load_state()
    admin = is_owner(m["from"]["id"], st)
    uid = m["from"]["id"]
    ses = session(uid)
    for k in list(ses.keys()):
        ses[k] = None if k not in ("svc_query", "broadcast_payload") else (
            "" if k == "svc_query" else {})
    await bot.send_message(m["chat"]["id"], "✅ Cancelled.",
                           reply_markup=reply_keyboard(admin))

async def cmd_balance(update: dict) -> None:
    m = update["message"]
    st = load_state()
    u = get_user(st, m["from"]["id"])
    admin = is_owner(m["from"]["id"], st)
    ref_av = referral_available(u)
    text = f"💰 <b>Your balance:</b> {fmt_ngn(balance(u))}"
    if ref_av > 0:
        text += (f"\n🎁 <b>Referral credit:</b> {fmt_ngn(ref_av)}"
                 f"\n\n🧮 <b>Total spendable:</b> {fmt_ngn(total_spendable(u))}")
    await bot.send_message(m["chat"]["id"], text, parse_mode="HTML",
                           reply_markup=reply_keyboard(admin))

async def cmd_history(update: dict) -> None:
    m = update["message"]
    st = load_state()
    u = get_user(st, m["from"]["id"])
    await send_history(m["chat"]["id"], st, u)

async def cmd_admin(update: dict) -> None:
    m = update["message"]
    st = load_state()
    if not is_owner(m["from"]["id"], st):
        await bot.send_message(m["chat"]["id"], "⛔ Owner only.")
        return
    await bot.send_message(m["chat"]["id"], "👑 <b>Admin panel</b>",
                           parse_mode="HTML", reply_markup=admin_menu(st))

async def cmd_paid(update: dict) -> None:
    m = update["message"]
    st = load_state()
    if not is_owner(m["from"]["id"], st):
        return
    parts = (m.get("text") or "").split()
    if len(parts) < 2:
        await bot.send_message(m["chat"]["id"],
                               "Usage: /paid <WITHDRAWAL_ID>")
        return
    wid = parts[1].strip().upper()
    found = False
    found_uid = None
    found_amount = 0.0
    for uid_str, u in st.get("users", {}).items():
        r = u.get("referral") or {}
        for e in (r.get("events") or []):
            if (e.get("withdrawal_id") == wid
                    and e.get("status") in ("pending", "processing")):
                amount = float(e.get("amount_ngn") or 0)
                r["reserved_ngn"] = max(0.0,
                                        float(r.get("reserved_ngn") or 0)
                                        - amount)
                r["paid_out_ngn"] = (float(r.get("paid_out_ngn") or 0)
                                     + amount)
                e["status"] = "paid"
                e["paid_ts"] = time.time()
                found = True
                found_uid = int(uid_str)
                found_amount = amount
                break
        if found:
            break
    if not found:
        await bot.send_message(m["chat"]["id"], "❌ Not found.")
        return
    save_state()
    if found_uid:
        try:
            await bot.send_message(
                found_uid,
                f"💵 <b>Withdrawal paid</b>\n\n"
                f"Amount: {fmt_ngn(found_amount)}\nID: {wid}",
                parse_mode="HTML")
        except Exception as ex:
            log.warning("paid DM: %s", ex)
    await bot.send_message(m["chat"]["id"], f"✅ {wid} marked paid.")

async def cmd_withdrawfail(update: dict) -> None:
    m = update["message"]
    st = load_state()
    if not is_owner(m["from"]["id"], st):
        return
    parts = (m.get("text") or "").split()
    if len(parts) < 2:
        await bot.send_message(m["chat"]["id"],
                               "Usage: /withdrawfail <ID> [reason]")
        return
    wid = parts[1].strip().upper()
    reason = " ".join(parts[2:]) or "processing error"
    found = False
    found_uid = None
    found_amount = 0.0
    for uid_str, u in st.get("users", {}).items():
        r = u.get("referral") or {}
        for e in (r.get("events") or []):
            if (e.get("withdrawal_id") == wid
                    and e.get("status") in ("pending", "processing")):
                amount = float(e.get("amount_ngn") or 0)
                r["reserved_ngn"] = max(0.0,
                                        float(r.get("reserved_ngn") or 0)
                                        - amount)
                e["status"] = "failed"
                e["failed_ts"] = time.time()
                e["fail_reason"] = reason
                found = True
                found_uid = int(uid_str)
                found_amount = amount
                break
        if found:
            break
    if not found:
        await bot.send_message(m["chat"]["id"], "❌ Not found.")
        return
    save_state()
    if found_uid:
        try:
            await bot.send_message(
                found_uid,
                f"⚠️ <b>Withdrawal failed</b>\n\n"
                f"Amount: {fmt_ngn(found_amount)}\n"
                f"Reason: {esc(reason)}\n\n"
                f"Returned to your referral pool.",
                parse_mode="HTML", reply_markup=back_menu())
        except Exception as ex:
            log.warning("wdfail DM: %s", ex)
    await bot.send_message(m["chat"]["id"], f"✅ {wid} failed, returned.")

async def cmd_reply(update: dict) -> None:
    m = update["message"]
    st = load_state()
    if not is_owner(m["from"]["id"], st):
        return
    parts = (m.get("text") or "").split(maxsplit=2)
    if len(parts) < 3:
        await bot.send_message(m["chat"]["id"],
                               "Usage: /reply <ticket> <message>")
        return
    ticket = parts[1].lstrip("#")
    body = parts[2]
    s = settings(st)
    comps = s.setdefault("complaints", {})
    c = comps.get(ticket)
    if not c:
        await bot.send_message(m["chat"]["id"], "❌ Ticket not found.")
        return
    c.setdefault("replies", []).append({
        "ts": time.time(), "text": body, "by": "admin",
    })
    c["status"] = "answered"
    save_state()
    try:
        await bot.send_message(
            int(c["uid"]),
            f"📬 <b>Admin reply to your complaint #{ticket}</b>\n\n"
            f"{esc(body)}",
            parse_mode="HTML", reply_markup=back_menu())
    except Exception as e:
        log.warning("reply DM: %s", e)
    await bot.send_message(m["chat"]["id"],
                           f"✅ Reply sent to user {c['uid']}.")

# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

async def on_callback(update: dict) -> None:
    cq = update["callback_query"]
    data = cq.get("data") or ""
    cb_id = cq["id"]
    uid = cq["from"]["id"]
    chat_id = cq["message"]["chat"]["id"]
    msg_id = cq["message"]["message_id"]
    st = load_state()
    u = get_user(st, uid)
    admin = is_owner(uid, st)

    try:
        await bot.answer_callback_query(cb_id)
    except Exception:
        pass

    if u.get("banned") and not admin:
        await _edit(chat_id, msg_id, "⛔ Account suspended.")
        return
    if data == "noop":
        return
    if data.startswith("admin:") and not admin:
        await _edit(chat_id, msg_id, "⛔ Owner only.")
        return

    if data == "menu:main":
        await _edit(chat_id, msg_id, welcome_text(u, admin),
                    reply_markup=main_menu(admin, u))
    elif data == "menu:help":
        await _edit(chat_id, msg_id, HELP_TEXT, reply_markup=back_menu())
    elif data == "menu:channels":
        text, kb = channels_card(st)
        await _edit(chat_id, msg_id, text, reply_markup=kb)
    elif data == "menu:support":
        text, kb = support_card(st)
        await _edit(chat_id, msg_id, text, reply_markup=kb)
    elif data == "support:complain":
        ses = session(uid)
        ses["state"] = "complaint_text"
        await _edit(chat_id, msg_id,
                    "✍️ <b>Send a Complaint</b>\n\n"
                    "Describe your problem in one message.\n\n"
                    "/cancel to abort.",
                    reply_markup=back_menu())
    elif data == "menu:balance":
        ref_av = referral_available(u)
        text = f"💰 <b>Your balance:</b> {fmt_ngn(balance(u))}"
        if ref_av > 0:
            text += (f"\n🎁 <b>Referral credit:</b> {fmt_ngn(ref_av)}"
                     f"\n\n🧮 <b>Total spendable:</b> "
                     f"{fmt_ngn(total_spendable(u))}")
        await _edit(chat_id, msg_id, text,
                    reply_markup=main_menu(admin, u))
    elif data == "menu:history":
        await send_history_cb(chat_id, msg_id, st, u)
    elif data == "menu:services":
        ses = session(uid)
        ses["svc_country"] = None
        ses["svc_query"] = ""
        await show_services(chat_id, msg_id, st, "", None, 0)
    elif data.startswith("svc:page:"):
        page = int(data.split(":")[2])
        ses = session(uid)
        await show_services(chat_id, msg_id, st,
                            ses.get("svc_query") or "",
                            ses.get("svc_country"), page)
    elif data.startswith("svc:country:"):
        c = data.split(":", 2)[2] or None
        ses = session(uid)
        ses["svc_country"] = c
        await show_services(chat_id, msg_id, st,
                            ses.get("svc_query") or "", c, 0)
    elif data == "svc:search":
        ses = session(uid)
        ses["state"] = "await_svc_query"
        await _edit(chat_id, msg_id,
                    "🔍 <b>Search</b>\n\n"
                    "Type what you need — e.g. <code>whatsapp</code>.\n\n"
                    "Send <code>*</code> to clear.",
                    reply_markup=back_menu())
    elif data.startswith("svc:pick:"):
        sid = int(data.split(":", 2)[2])
        await confirm_rent(chat_id, msg_id, st, u, sid)
    elif data.startswith("svc:rent:"):
        sid = int(data.split(":", 2)[2])
        await do_rent(chat_id, msg_id, st, u, sid)
    elif data == "menu:active":
        await show_active(chat_id, msg_id, st, u)
    elif data.startswith("act:view:"):
        rid = int(data.split(":", 2)[2])
        await view_rental(chat_id, msg_id, st, u, rid)
    elif data.startswith("act:refresh:"):
        rid = int(data.split(":", 2)[2])
        await refresh_rental(chat_id, msg_id, st, u, rid)
    elif data.startswith("act:cancel:"):
        rid = int(data.split(":", 2)[2])
        await do_cancel(chat_id, msg_id, st, u, rid)
    elif data.startswith("act:done:"):
        rid = int(data.split(":", 2)[2])
        await do_done(chat_id, msg_id, st, u, rid)
    elif data == "menu:topup":
        await start_deposit_flow(chat_id, msg_id, uid, st, u)
    elif data == "topup:bank":
        await start_deposit_flow(chat_id, msg_id, uid, st, u)
    elif data == "topup:confirm_paid":
        await confirm_deposit_paid(chat_id, msg_id, uid, st, u)
    elif data == "menu:referral":
        await show_referral(chat_id, msg_id, st, u)
    elif data == "ref:withdraw":
        await request_referral_withdrawal(chat_id, msg_id, uid, st, u)
    elif data == "ref:withdraw_confirm":
        await confirm_referral_withdrawal(chat_id, msg_id, uid, st, u)
    elif data == "ref:history":
        await show_referral_history(chat_id, msg_id, st, u)
    elif data == "menu:pending":
        await show_pending(chat_id, msg_id, st, u)
    elif data.startswith("admin:"):
        await handle_admin_cb(chat_id, msg_id, uid, st, u, data)

async def _edit(chat_id: int, message_id: int, text: str,
                reply_markup: dict | None = None) -> None:
    try:
        await bot.edit_message_text(chat_id, message_id, text,
                                    parse_mode="HTML",
                                    reply_markup=reply_markup)
    except Exception as e:
        err = str(e)
        if "message is not modified" in err:
            return
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML",
                                   reply_markup=reply_markup)
        except Exception as e2:
            log.debug("edit+fallback failed: %s / %s", e, e2)

# ---------------------------------------------------------------------------
# Services browsing
# ---------------------------------------------------------------------------

def services_keyboard(st: dict, services: list[dict], page: int,
                      page_size: int, country: str | None) -> dict:
    rows: list[list[dict]] = []
    start = page * page_size
    chunk = services[start:start + page_size]
    for s in chunk:
        sid = s.get("id")
        price = price_in_ngn(st, sid)
        if price is None:
            continue
        label = f"{s.get('name')} · {s.get('country')} · {fmt_ngn(price)}"
        rows.append([ibtn(shorten(label, 55), f"svc:pick:{sid}")])

    nav = []
    total_pages = max(1, (len(services) + page_size - 1) // page_size)
    if page > 0:
        nav.append(ibtn("◀ Prev", f"svc:page:{page - 1}"))
    nav.append(ibtn(f"{page + 1}/{total_pages}", f"svc:page:{page}"))
    if page < total_pages - 1:
        nav.append(ibtn("Next ▶", f"svc:page:{page + 1}"))
    if nav:
        rows.append(nav)

    countries = sorted({s.get("country") for s in services if s.get("country")})
    if countries:
        c = country or "ALL"
        rows.append([ibtn(f"🌍 {c}",
                          f"svc:country:{'' if c == 'ALL' else c}")])
    rows.append([ibtn("🔍 Search", "svc:search")])
    rows.append([ibtn("⬅️ Main menu", "menu:main")])
    return inline_kb(rows)

async def show_services(chat_id: int, msg_id: int, st: dict, query: str,
                        country: str | None, page: int) -> None:
    await _edit(chat_id, msg_id, "⏳ <b>Loading services…</b>")
    services = await fetch_services_cached(st)
    if country:
        services = [s for s in services
                    if str(s.get("country") or "").upper() == country.upper()]
    if query:
        ql = query.lower()
        services = [s for s in services
                    if ql in str(s.get("name") or "").lower()]
    sellable = [s for s in services if price_in_ngn(st, s.get("id")) is not None]
    if not sellable:
        await _edit(chat_id, msg_id,
                    "📱 <b>No services available right now</b>",
                    reply_markup=inline_kb([
                        [ibtn("🔍 Search", "svc:search")],
                        [ibtn("🌍 All countries", "svc:country:")],
                        [ibtn("⬅️ Main menu", "menu:main")],
                    ]))
        return
    head = (f"📱 <b>Available Services</b>\n"
            + (f"🌍 Country: {esc(country)}\n" if country else "")
            + f"📦 Total: <b>{len(sellable)}</b>\n\n"
            "Tap a service to rent a number 👇")
    await _edit(chat_id, msg_id, head,
                reply_markup=services_keyboard(st, sellable, page, 6, country))

async def confirm_rent(chat_id: int, msg_id: int, st: dict, u: dict,
                       sid: int) -> None:
    await _edit(chat_id, msg_id, "⏳ <b>Loading service…</b>")
    services = await fetch_services_cached(st)
    match = next((s for s in services if s.get("id") == sid), None)
    if not match:
        await _edit(chat_id, msg_id, "⚠️ Service unavailable.",
                    reply_markup=back_menu())
        return
    price = price_in_ngn(st, sid)
    if price is None:
        await _edit(chat_id, msg_id, "⚠️ Not for sale.",
                    reply_markup=back_menu())
        return
    spendable = total_spendable(u)
    can_afford = spendable >= price
    rows = []
    if can_afford:
        rows.append([ibtn(f"✅ Buy for {fmt_ngn(price)}", f"svc:rent:{sid}")])
    else:
        need = price - spendable
        rows.append([ibtn(f"💳 Add {fmt_ngn(need)} to buy", "menu:topup")])
    rows.append([ibtn("⬅️ Back", "menu:services")])
    rows.append([ibtn("⬅️ Main menu", "menu:main")])
    ref_av = referral_available(u)
    text = (
        f"📱 <b>{esc(match.get('name'))}</b>\n"
        f"🌍 Country: {esc(match.get('country_name'))}\n\n"
        f"💵 Price: <b>{fmt_ngn(price)}</b>\n"
        f"💰 Balance: <b>{fmt_ngn(balance(u))}</b>\n"
    )
    if ref_av > 0:
        text += f"🎁 Referral credit: <b>{fmt_ngn(ref_av)}</b>\n"
    text += f"\n🧮 Total spendable: <b>{fmt_ngn(spendable)}</b>\n"
    if can_afford:
        text += f"✅ After purchase: <b>{fmt_ngn(spendable - price)}</b> left."
    else:
        text += f"⚠️ You need <b>{fmt_ngn(price - spendable)}</b> more."
    await _edit(chat_id, msg_id, text, reply_markup=inline_kb(rows))

# ---------------------------------------------------------------------------
# Rent flow
# ---------------------------------------------------------------------------

async def do_rent(chat_id: int, msg_id: int, st: dict, u: dict,
                  sid: int) -> None:
    s = settings(st)
    if s.get("maintenance") and not is_owner(u["uid"], st):
        await _edit(chat_id, msg_id, "🚧 Maintenance. Try again later.")
        return
    if not rate_ok(u, int(s.get("max_per_minute") or 10)):
        await _edit(chat_id, msg_id, "⏳ Too fast. Wait a minute.")
        return
    if len(u.get("active_rentals") or {}) >= MAX_ACTIVE_PER_USER:
        await _edit(chat_id, msg_id,
                    f"⚠️ You already have {MAX_ACTIVE_PER_USER} active "
                    "numbers.\nWait for one to finish first.")
        return

    await _edit(chat_id, msg_id, "⏳ <b>Preparing your number…</b>")
    services = await fetch_services_cached(st)
    match = next((x for x in services if x.get("id") == sid), None)
    if not match:
        await _edit(chat_id, msg_id, "⚠️ Service unavailable.",
                    reply_markup=back_menu())
        return
    price = price_in_ngn(st, sid)
    if price is None:
        await _edit(chat_id, msg_id, "⚠️ Not for sale.",
                    reply_markup=back_menu())
        return
    if total_spendable(u) < price:
        await _edit(chat_id, msg_id,
                    f"💳 Not enough spendable.\n"
                    f"Need {fmt_ngn(price)}, have {fmt_ngn(total_spendable(u))}.",
                    reply_markup=main_menu(is_owner(u["uid"], st), u))
        return
    provider_price = float(match.get("price") or 0)
    buffer_ngn = float(s.get("safety_buffer_ngn") or 0)
    my_balance = await get_provider_balance(st)
    if my_balance < (provider_price + buffer_ngn):
        await _enqueue_pending(chat_id, msg_id, st, u, sid, match, price,
                                provider_price)
        return
    await _execute_rent(chat_id, msg_id, st, u, sid, match, price,
                        provider_price, from_queue=False)

async def _execute_rent(chat_id: int | None, msg_id: int | None, st: dict,
                        u: dict, sid: int, match: dict, price_ngn: float,
                        provider_price: float,
                        from_queue: bool = False) -> bool:
    idem = f"tg-{u['uid']}-{uuid.uuid4().hex[:16]}"
    try:
        resp = await asyncio.to_thread(lambda: gateway(st).rent(sid, idem))
    except TextSMSError as e:
        if e.code == "insufficient_balance":
            if from_queue:
                return False
            await get_provider_balance(st, force=True)
            if chat_id and msg_id:
                await _enqueue_pending(chat_id, msg_id, st, u, sid, match,
                                        price_ngn, provider_price)
            return False
        if e.code in ("provider_error", "in_progress", "rate_limited"):
            if chat_id and msg_id:
                await _edit(chat_id, msg_id,
                            "⏳ Supplier busy. Your request is queued...",
                            reply_markup=main_menu(is_owner(u["uid"], st), u))
            return False
        if chat_id and msg_id:
            await _edit(chat_id, msg_id,
                        f"❌ <b>Purchase failed</b>\n"
                        f"{esc(e.code)}\n{esc(e.message)}",
                        reply_markup=back_menu())
        return False

    rental = resp.get("rental") or {}
    rid = rental.get("id")
    if not rid:
        if chat_id and msg_id:
            await _edit(chat_id, msg_id, "⚠️ No rental id in response.",
                        reply_markup=back_menu())
        return False

    # charge BEFORE marking successful_rentals so referral can see
    # "done == 1" only after a successful rental (rental exists).
    charge_src = None
    if not from_queue:
        charge_src = charge_balance(u, price_ngn)

    # increment successful_rentals AFTER the charge — this is what triggers
    # the referral bonus for the inviter (buyer's first purchase).
    prev_done = int(u.get("successful_rentals") or 0)
    u["successful_rentals"] = prev_done + 1

    u.setdefault("active_rentals", {})[str(rid)] = {
        "id": rid,
        "order_id": rental.get("order_id"),
        "service": rental.get("service"),
        "service_code": rental.get("service_code"),
        "number": rental.get("number"),
        "status": rental.get("status"),
        "provider_price_ngn": rental.get("price"),
        "price_ngn": price_ngn,
        "country": match.get("country_name"),
        "rented_at": rental.get("rented_at"),
        "expires_in": rental.get("expires_in"),
        "code": "",
        "started_ts": time.time(),
        "waiting_reminder_sent": False,
        "charge_source": charge_src or {"from_balance": 0.0,
                                         "from_referral": 0.0},
    }

    bonus = apply_referral_bonus(st, u["uid"], rid)
    save_state()

    if bonus:
        try:
            await bot.send_message(
                bonus["inviter_uid"],
                f"🎉 <b>Referral bonus!</b>\n\n"
                f"A friend you invited just made their first purchase.\n\n"
                f"💸 Base: {fmt_ngn(bonus['base'])}\n"
                f"🎁 Extra: {fmt_ngn(bonus['extra'])}\n"
                f"💰 <b>Total credited: {fmt_ngn(bonus['amount'])}</b>\n\n"
                f"🧮 Referral balance: "
                f"<b>{fmt_ngn(bonus['inviter_available'])}</b>\n\n"
                f"Spend it on a number or withdraw it from 🎁 Referral.",
                parse_mode="HTML",
                reply_markup=inline_kb([
                    [ibtn("📱 Buy with credit", "menu:services")],
                    [ibtn("💵 Withdraw", "ref:withdraw")],
                    [ibtn("🎁 Referral", "menu:referral")],
                ]))
        except Exception as e:
            log.warning("referral DM: %s", e)

    if from_queue:
        try:
            await bot.send_message(
                u["uid"],
                f"✅ <b>Server live — your number is ready</b>\n\n"
                f"📱 {esc(rental.get('service'))}\n"
                f"🌍 {esc(match.get('country_name'))}\n"
                f"📞 Number: <code>{esc(rental.get('number'))}</code>\n"
                f"💵 Paid: {fmt_ngn(price_ngn)}\n"
                f"🧮 Spendable: <b>{fmt_ngn(total_spendable(u))}</b>\n\n"
                "Use this number in the app — we'll send the code the "
                "moment it arrives.",
                parse_mode="HTML",
                reply_markup=inline_kb([
                    [ibtn("🔄 Refresh", f"act:refresh:{rid}")],
                    [ibtn("📋 My numbers", "menu:active")],
                ]))
        except Exception as e:
            log.warning("queue DM: %s", e)
    elif chat_id and msg_id:
        await _edit(chat_id, msg_id,
                    f"✅ <b>Number ready</b>\n\n"
                    f"📱 <b>{esc(rental.get('service'))}</b>\n"
                    f"🌍 {esc(match.get('country_name'))}\n"
                    f"📞 Number: <code>{esc(rental.get('number'))}</code>\n"
                    f"💵 Paid: <b>{fmt_ngn(price_ngn)}</b>\n"
                    f"🧮 Spendable: <b>{fmt_ngn(total_spendable(u))}</b>\n\n"
                    "Use this number in the app — we'll send the code the "
                    "moment it arrives.",
                    reply_markup=inline_kb([
                        [ibtn("🔄 Refresh", f"act:refresh:{rid}")],
                        [ibtn("📋 My numbers", "menu:active")],
                        [ibtn("⬅️ Main menu", "menu:main")],
                    ]))

    asyncio.create_task(_poll_rental(u["uid"], rid))
    return True

async def _enqueue_pending(chat_id: int, msg_id: int, st: dict, u: dict,
                            sid: int, match: dict, price_ngn: float,
                            provider_price: float) -> None:
    pending = u.setdefault("pending_rentals", [])
    if any(p.get("service_id") == sid for p in pending):
        await _edit(chat_id, msg_id,
                    "⏳ This service is already in your queue.",
                    reply_markup=main_menu(is_owner(u["uid"], st), u))
        return
    src = charge_balance(u, price_ngn)
    pending.append({
        "id": str(uuid.uuid4()),
        "service_id": sid,
        "service": match.get("name"),
        "country": match.get("country_name"),
        "price_ngn": price_ngn,
        "provider_price_ngn": provider_price,
        "queued_ts": time.time(),
        "state": "queued",
        "attempts": 0,
        "charge_source": src,
    })
    save_state()

    await _edit(chat_id, msg_id,
                f"⏳ <b>Please wait — server temporarily down</b>\n\n"
                f"📱 {esc(match.get('name'))} · "
                f"{esc(match.get('country_name'))}\n"
                f"💵 {fmt_ngn(price_ngn)} held\n\n"
                f"Supplier is briefly out of stock. Wait <b>10–20 "
                f"minutes</b> — we'll message you the moment your number "
                f"is ready.\n\n"
                f"No completion in 30 minutes → automatic refund.",
                reply_markup=inline_kb([
                    [ibtn("📋 Processing", "menu:pending")],
                    [ibtn("⬅️ Main menu", "menu:main")],
                ]))

    await _alert_admin_low_wallet(st, u, match, price_ngn, provider_price)

async def _alert_admin_low_wallet(st: dict, u: dict, match: dict,
                                   price_ngn: float,
                                   provider_price: float) -> None:
    s = settings(st)
    oid = int(s.get("owner_id") or 0)
    if not oid:
        return
    now = time.time()
    last = float(s.get("admin_low_wallet_alert_ts") or 0)
    if now - last < ADMIN_ALERT_COOLDOWN_SECONDS:
        return
    s["admin_low_wallet_alert_ts"] = now
    try:
        my_balance = await get_provider_balance(st, force=True)
    except Exception:
        my_balance = 0.0
    needed = provider_price + float(s.get("safety_buffer_ngn") or 0)
    short = max(0.0, needed - my_balance)
    try:
        await bot.send_message(
            oid,
            f"🚨 <b>LOW WALLET — restock needed</b>\n\n"
            f"User <code>{u['uid']}</code> "
            f"({esc(u.get('name') or '—')}) wants to buy "
            f"<b>{esc(match.get('name'))} · {esc(match.get('country_name'))}</b>.\n\n"
            f"💵 Buyer paid: {fmt_ngn(price_ngn)}\n"
            f"🏦 Supplier cost: {fmt_ngn(provider_price)}\n"
            f"💰 Your wallet: <b>{fmt_ngn(my_balance)}</b>\n"
            f"⚠️ Short by: <b>{fmt_ngn(short)}</b>\n\n"
            f"Restock at textsms.io. Bot retries automatically. "
            f"30-minute refund if no restock.",
            parse_mode="HTML")
    except Exception as e:
        log.warning("admin alert failed: %s", e)

# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------

_POLL_TASKS: dict[str, asyncio.Task] = {}

async def _poll_rental(uid: int, rid: int) -> None:
    key = f"{uid}:{rid}"
    if key in _POLL_TASKS:
        return
    task = asyncio.current_task()
    if task:
        _POLL_TASKS[key] = task
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            st = load_state()
            u = get_user(st, uid)
            rental = (u.get("active_rentals") or {}).get(str(rid))
            if not rental:
                return

            started = float(rental.get("started_ts") or 0)
            if (not rental.get("waiting_reminder_sent")
                    and time.time() - started > 300
                    and not (rental.get("code") or "").strip()):
                rental["waiting_reminder_sent"] = True
                save_state()
                try:
                    await bot.send_message(
                        uid,
                        f"⏳ <b>Still waiting for your code</b>\n\n"
                        f"📱 {esc(rental.get('service'))}\n"
                        f"📞 <code>{esc(rental.get('number'))}</code>\n\n"
                        f"Most codes arrive in 1–2 min. No code → auto-refund.",
                        parse_mode="HTML",
                        reply_markup=inline_kb([
                            [ibtn("🔄 Refresh", f"act:refresh:{rid}")],
                        ]))
                except Exception:
                    pass

            try:
                resp = await asyncio.to_thread(lambda: gateway(st).status(rid))
            except TextSMSError as e:
                if e.code in ("not_found", "unauthenticated"):
                    return
                continue
            r = resp.get("rental") or {}
            code = r.get("code") or ""
            status = r.get("status") or ""
            expires_in = resp.get("expires_in", rental.get("expires_in") or 0)

            if code:
                rental["code"] = code
                rental["status"] = status
                rental["expires_in"] = 0
                rental["auto_finished"] = True
                save_state()
                try:
                    await asyncio.to_thread(lambda: gateway(st).finish(rid))
                except Exception as e:
                    log.warning("auto-finish failed: %s", e)
                try:
                    await bot.send_message(
                        uid,
                        f"🎉 <b>Code received!</b>\n\n"
                        f"📱 {esc(rental.get('service'))}\n"
                        f"📞 <code>{esc(rental.get('number'))}</code>\n\n"
                        f"<b>Your code:</b>\n<code>{esc(code)}</code>\n\n"
                        f"Number closed. Charged once. Enjoy!",
                        parse_mode="HTML",
                        reply_markup=inline_kb([
                            [ibtn("✅ Done", f"act:done:{rid}")],
                            [ibtn("📱 Buy another", "menu:services")],
                        ]))
                except Exception as e:
                    log.warning("code DM: %s", e)
                return

            if status == "cancelled":
                refund = float(rental.get("price_ngn") or 0)
                refund_balance(u, refund, source=rental.get("charge_source"))
                _finalize(u, rid, "cancelled", refund)
                save_state()
                try:
                    await bot.send_message(
                        uid,
                        f"⏱ <b>No code — refunded</b>\n\n"
                        f"📞 <code>{esc(rental.get('number'))}</code>\n"
                        f"💵 Refunded: <b>{fmt_ngn(refund)}</b>\n"
                        f"🧮 Spendable: {fmt_ngn(total_spendable(u))}",
                        parse_mode="HTML", reply_markup=back_menu())
                except Exception:
                    pass
                return

            rental["status"] = status
            rental["expires_in"] = expires_in
            save_state()
    finally:
        _POLL_TASKS.pop(key, None)

async def resume_all_pollers() -> None:
    st = load_state()
    for uid_str, u in st.get("users", {}).items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        for rid_str in (u.get("active_rentals") or {}):
            try:
                rid = int(rid_str)
            except ValueError:
                continue
            asyncio.create_task(_poll_rental(uid, rid))

# ---------------------------------------------------------------------------
# Active / history / cancel / done
# ---------------------------------------------------------------------------

def _cancel_countdown(r: dict) -> int:
    started = float(r.get("started_ts") or 0)
    return max(0, int(120 - (time.time() - started)))

async def show_active(chat_id: int, msg_id: int, st: dict, u: dict) -> None:
    rentals = u.get("active_rentals") or {}
    if not rentals:
        await _edit(chat_id, msg_id,
                    "🔄 <b>My Numbers</b>\n\nNo open numbers.\n\n"
                    "Tap 📱 Buy to rent a number.",
                    reply_markup=main_menu(is_owner(u["uid"], st), u))
        return
    rows = []
    lines = []
    for rid_str, r in rentals.items():
        try:
            rid = int(rid_str)
        except ValueError:
            continue
        code = (r.get("code") or "").strip()
        status = "✅ Code received" if code else "⏳ Waiting"
        lines.append(f"• {esc(r.get('service'))} · "
                     f"<code>{esc(r.get('number'))}</code> · {status}")
        rows.append([ibtn(f"👁 {shorten(str(r.get('service')), 20)}",
                          f"act:view:{rid}")])
    rows.append([ibtn("⬅️ Main menu", "menu:main")])
    await _edit(chat_id, msg_id,
                "🔄 <b>My Numbers</b>\n\n" + "\n".join(lines),
                reply_markup=inline_kb(rows))

async def view_rental(chat_id: int, msg_id: int, st: dict, u: dict,
                      rid: int) -> None:
    r = (u.get("active_rentals") or {}).get(str(rid))
    if not r:
        await _edit(chat_id, msg_id,
                    "⚠️ Number not found — may have closed.",
                    reply_markup=main_menu(is_owner(u["uid"], st), u))
        return
    code = (r.get("code") or "").strip()
    lines = [
        f"📱 <b>{esc(r.get('service'))}</b>",
        f"🌍 {esc(r.get('country') or '—')}",
        f"📞 Number: <code>{esc(r.get('number'))}</code>",
        f"💵 Paid: {fmt_ngn(r.get('price_ngn'))}",
    ]
    if code:
        lines.append(f"\n<b>Your code:</b>\n<code>{esc(code)}</code>")
    else:
        lines.append(f"\n⏳ Status: <b>{esc(r.get('status') or 'waiting')}</b>")
    if r.get("expires_in"):
        lines.append(f"⏱ Expires in: {r.get('expires_in')}s")
    rows = []
    if code:
        rows.append([ibtn("✅ Done", f"act:done:{rid}")])
    else:
        rows.append([ibtn("🔄 Refresh", f"act:refresh:{rid}")])
        remaining = _cancel_countdown(r)
        if remaining > 0:
            m, s = divmod(remaining, 60)
            rows.append([ibtn(f"❌ Cancel opens in {m}m{s:02d}s", "noop")])
        else:
            rows.append([ibtn("❌ Cancel (refund)", f"act:cancel:{rid}")])
    rows.append([ibtn("⬅️ My numbers", "menu:active")])
    rows.append([ibtn("⬅️ Main menu", "menu:main")])
    await _edit(chat_id, msg_id, "\n".join(lines),
                reply_markup=inline_kb(rows))

async def refresh_rental(chat_id: int, msg_id: int, st: dict, u: dict,
                          rid: int) -> None:
    await _edit(chat_id, msg_id, "⏳ <b>Checking…</b>")
    try:
        resp = await asyncio.to_thread(lambda: gateway(st).status(rid))
    except TextSMSError as e:
        await _edit(chat_id, msg_id,
                    f"❌ {esc(e.code)} — {esc(e.message)}",
                    reply_markup=back_menu())
        return
    r = resp.get("rental") or {}
    code = r.get("code") or ""
    rental = (u.get("active_rentals") or {}).get(str(rid))
    if rental and code:
        rental["code"] = code
        rental["status"] = r.get("status") or rental.get("status")
        save_state()
    await view_rental(chat_id, msg_id, st, u, rid)

async def do_cancel(chat_id: int, msg_id: int, st: dict, u: dict,
                     rid: int) -> None:
    rental = (u.get("active_rentals") or {}).get(str(rid))
    if not rental:
        await _edit(chat_id, msg_id, "⚠️ Not found.",
                    reply_markup=back_menu())
        return
    await _edit(chat_id, msg_id, "⏳ <b>Cancelling…</b>")
    try:
        await asyncio.to_thread(lambda: gateway(st).cancel(rid))
    except TextSMSError as e:
        if e.code == "too_early":
            remaining = _cancel_countdown(rental)
            m, s = divmod(remaining, 60)
            await _edit(chat_id, msg_id,
                        f"⏱ <b>Too early</b>\n\nOpens in {m}m{s:02d}s.",
                        reply_markup=inline_kb([
                            [ibtn("⬅️ Back", f"act:view:{rid}")],
                        ]))
        elif e.code == "code_received":
            await _edit(chat_id, msg_id,
                        "⚠️ Code already arrived. Use ✅ Done.",
                        reply_markup=inline_kb([
                            [ibtn("✅ Done", f"act:done:{rid}")],
                        ]))
        elif e.code == "already_closed":
            refund = float(rental.get("price_ngn") or 0)
            refund_balance(u, refund, source=rental.get("charge_source"))
            _finalize(u, rid, "cancelled", refund)
            save_state()
            await _edit(chat_id, msg_id,
                        "ℹ️ Already closed. Refund applied.",
                        reply_markup=back_menu())
        else:
            await _edit(chat_id, msg_id,
                        f"❌ {esc(e.code)} — {esc(e.message)}",
                        reply_markup=back_menu())
        return
    refund = float(rental.get("price_ngn") or 0)
    refund_balance(u, refund, source=rental.get("charge_source"))
    _finalize(u, rid, "cancelled", refund)
    save_state()
    await _edit(chat_id, msg_id,
                f"✅ <b>Cancelled</b>\n\n"
                f"📞 {esc(rental.get('number'))}\n"
                f"💵 Refunded: {fmt_ngn(refund)}\n"
                f"🧮 Spendable: {fmt_ngn(total_spendable(u))}",
                reply_markup=back_menu())

async def do_done(chat_id: int, msg_id: int, st: dict, u: dict,
                  rid: int) -> None:
    rental = (u.get("active_rentals") or {}).get(str(rid))
    if rental:
        _finalize(u, rid, "received", 0)
        save_state()
    text = thank_you_card(u)
    await _edit(chat_id, msg_id, text,
                reply_markup=inline_kb([
                    [ibtn("📱 Buy another number", "menu:services")],
                    [ibtn("🆘 Support", "menu:support")],
                    [ibtn("⬅️ Main menu", "menu:main")],
                ]))

def _finalize(u: dict, rid: int, final_status: str, refund_ngn: float) -> None:
    rental = (u.get("active_rentals") or {}).pop(str(rid), None)
    if not rental:
        return
    rental["status"] = final_status
    rental["closed_ts"] = time.time()
    if refund_ngn:
        rental["refund_ngn"] = refund_ngn
    u.setdefault("history", []).append(rental)
    if len(u["history"]) > 300:
        u["history"] = u["history"][-300:]

async def send_history(chat_id: int, st: dict, u: dict) -> None:
    items = list(u.get("history") or [])[-15:][::-1]
    if not items:
        await bot.send_message(chat_id, "📜 <b>History</b>\n\nNo past purchases.",
                               parse_mode="HTML", reply_markup=back_menu())
        return
    lines = []
    for h in items:
        icon = "✅" if h.get("status") == "received" else "❌"
        lines.append(f"{icon} {ts_str(h.get('closed_ts') or h.get('ts'))} · "
                     f"{esc(h.get('service'))} · "
                     f"{esc(h.get('number'))} · "
                     f"{fmt_ngn(h.get('price_ngn'))}")
    await bot.send_message(chat_id,
                           "📜 <b>Your History</b>\n\n" + "\n".join(lines),
                           parse_mode="HTML", reply_markup=back_menu())

async def send_history_cb(chat_id: int, msg_id: int, st: dict,
                          u: dict) -> None:
    items = list(u.get("history") or [])[-15:][::-1]
    if not items:
        await _edit(chat_id, msg_id,
                    "📜 <b>History</b>\n\nNo past purchases.",
                    reply_markup=main_menu(is_owner(u["uid"], st), u))
        return
    lines = []
    for h in items:
        icon = "✅" if h.get("status") == "received" else "❌"
        lines.append(f"{icon} {ts_str(h.get('closed_ts') or h.get('ts'))} · "
                     f"{esc(h.get('service'))} · "
                     f"{esc(h.get('number'))} · "
                     f"{fmt_ngn(h.get('price_ngn'))}")
    await _edit(chat_id, msg_id,
                "📜 <b>Your History</b>\n\n" + "\n".join(lines),
                reply_markup=main_menu(is_owner(u["uid"], st), u))

# ---------------------------------------------------------------------------
# Pending queue view
# ---------------------------------------------------------------------------

async def show_pending(chat_id: int, msg_id: int, st: dict,
                       u: dict) -> None:
    pending = u.get("pending_rentals") or []
    if not pending:
        await _edit(chat_id, msg_id,
                    "⏳ <b>Processing</b>\n\nNothing queued.",
                    reply_markup=main_menu(is_owner(u["uid"], st), u))
        return
    lines = []
    for p in pending:
        age = int((time.time() - float(p.get("queued_ts") or 0)) / 60)
        lines.append(f"⏳ {esc(p.get('service'))} · "
                     f"{esc(p.get('country'))}\n"
                     f"   Queued {age}m · {fmt_ngn(p.get('price_ngn'))} held")
    await _edit(chat_id, msg_id,
                "⏳ <b>Processing</b>\n\n" + "\n\n".join(lines) +
                "\n\nAuto-refund if not completed in 30 minutes.",
                reply_markup=main_menu(is_owner(u["uid"], st), u))

# ---------------------------------------------------------------------------
# Deposit flow (3 steps)
# ---------------------------------------------------------------------------

async def start_deposit_flow(chat_id: int, msg_id: int, uid: int,
                              st: dict, u: dict) -> None:
    cfg = settings(st).get("bank") or {}
    if not cfg.get("enabled"):
        await _edit(chat_id, msg_id,
                    "💳 <b>Deposits temporarily disabled</b>",
                    reply_markup=back_menu())
        return
    ses = session(uid)
    ses["state"] = "dep_amount"
    ses.pop("dep_amount", None)
    await _edit(chat_id, msg_id,
                f"💳 <b>Add Money</b>\n\n"
                f"💰 Current balance: <b>{fmt_ngn(balance(u))}</b>\n\n"
                f"<b>Step 1/3 — Transfer to this account:</b>\n"
                f"🏛 {esc(cfg.get('bank_name'))}\n"
                f"🔢 <code>{esc(cfg.get('account_number'))}</code>\n"
                f"👤 {esc(cfg.get('account_name'))}\n\n"
                f"Minimum: {fmt_ngn(cfg.get('min_deposit_ngn') or 1000)}\n\n"
                f"After transferring, type the amount you sent "
                f"(e.g. <code>4000</code>):",
                reply_markup=inline_kb([
                    [ibtn("❌ Cancel", "menu:main")],
                ]))

async def confirm_deposit_paid(chat_id: int, msg_id: int, uid: int,
                                st: dict, u: dict) -> None:
    ses = session(uid)
    amount = float(ses.get("dep_amount") or 0)
    if amount <= 0:
        await _edit(chat_id, msg_id,
                    "❌ Something went wrong. Tap 💳 Add Money to restart.",
                    reply_markup=back_menu())
        return
    ses["state"] = "dep_screenshot"
    await _edit(chat_id, msg_id,
                f"✅ Amount: <b>{fmt_ngn(amount)}</b>\n\n"
                f"<b>Step 3/3 — Send a screenshot of your bank receipt.</b>\n\n"
                f"Should show: amount, recipient 9167685658, date/time.\n\n"
                f"Send the screenshot here.",
                reply_markup=inline_kb([
                    [ibtn("❌ Cancel", "menu:main")],
                ]))

# ---------------------------------------------------------------------------
# Referral UI + withdrawal
# ---------------------------------------------------------------------------

def referral_card(st: dict, u: dict) -> tuple[str, dict]:
    r = ensure_referral(u)
    cfg = settings(st).get("referral") or {}
    enabled = bool(cfg.get("enabled"))
    link = f"https://t.me/{bot.username}?start=ref_{r['code']}"
    share_text = "Best SMS verification bot — real numbers, instant delivery:"
    share_url = ("https://t.me/share/url"
                 f"?url={quote(link, safe='')}"
                 f"&text={quote(share_text, safe='')}")
    invited = sum(1 for o in st.get("users", {}).values()
                  if str((o.get("referral") or {}).get("referred_by") or "") == str(u["uid"]))
    converted = sum(1 for o in st.get("users", {}).values()
                    if str((o.get("referral") or {}).get("referred_by") or "") == str(u["uid"])
                    and int(o.get("successful_rentals") or 0) >= 1)
    earned = float(r.get("earnings_ngn") or 0)
    spent = float(r.get("spent_on_numbers_ngn") or 0)
    paid = float(r.get("paid_out_ngn") or 0)
    reserved = float(r.get("reserved_ngn") or 0)
    available = referral_available(u)

    base = float(cfg.get("bonus_first_ngn") or 200)
    extra = float(cfg.get("bonus_extra_ngn") or 100)
    per_ref = base + extra
    min_w = float(cfg.get("min_withdraw_ngn") or 500)
    can_withdraw = enabled and available >= min_w

    if not enabled:
        status = ("🔴 <b>Referral program: PAUSED</b>\n\n"
                  "Earning is off. Your link still tracks signups — "
                  "bonuses resume when admin re-enables it.")
    elif can_withdraw:
        status = "🟢 <b>Referral program: ACTIVE</b>\n\n✅ You can withdraw now."
    else:
        status = (f"🟢 <b>Referral program: ACTIVE</b>\n\n"
                  f"📊 Minimum withdrawal: <b>{fmt_ngn(min_w)}</b>.\n"
                  f"💡 Or just spend your credit on a number — no "
                  f"withdrawal needed.")

    text = (
        f"🎁 <b>Referral Program</b>\n\n"
        f"Earn <b>{fmt_ngn(per_ref)}</b> when a friend you invite makes "
        f"their <b>first purchase</b>.\n"
        f"  └ Base {fmt_ngn(base)} + bonus {fmt_ngn(extra)}\n\n"
        f"💡 Spend it on numbers OR withdraw it to your bank.\n\n"
        f"<b>Your link:</b>\n<code>{esc(link)}</code>\n\n"
        f"<b>Your Stats</b>\n"
        f"👥 Signed up: <b>{invited}</b> · ✅ Bought: <b>{converted}</b>\n"
        f"💸 Total earned: <b>{fmt_ngn(earned)}</b>\n"
        f"🛍 Spent on numbers: <b>{fmt_ngn(spent)}</b>\n"
        f"💵 Withdrawn: <b>{fmt_ngn(paid)}</b>\n"
        f"⏳ In-flight withdrawal: <b>{fmt_ngn(reserved)}</b>\n"
        f"🟢 <b>Available to spend/withdraw: {fmt_ngn(available)}</b>\n\n"
        f"{status}"
    )
    rows = [
        [{"text": "📤 Share your link", "url": share_url}],
    ]
    if enabled and available >= min_w:
        rows.append([ibtn("💵 Withdraw", "ref:withdraw")])
    rows.append([ibtn("📜 History", "ref:history")])
    rows.append([ibtn("⬅️ Main menu", "menu:main")])
    return text, inline_kb(rows)

async def show_referral(chat_id: int, msg_id: int, st: dict, u: dict) -> None:
    text, kb = referral_card(st, u)
    await _edit(chat_id, msg_id, text, reply_markup=kb)

async def show_referral_msg(chat_id: int, st: dict, u: dict) -> None:
    text, kb = referral_card(st, u)
    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)

async def request_referral_withdrawal(chat_id: int, msg_id: int, uid: int,
                                       st: dict, u: dict) -> None:
    r = ensure_referral(u)
    cfg = settings(st).get("referral") or {}
    if not cfg.get("enabled"):
        await _edit(chat_id, msg_id,
                    "⚠️ Referral program is paused. Try again later.",
                    reply_markup=back_menu())
        return
    available = referral_available(u)
    min_w = float(cfg.get("min_withdraw_ngn") or 500)
    if available < min_w:
        await _edit(chat_id, msg_id,
                    f"🔒 <b>Withdrawal locked</b>\n\n"
                    f"Minimum: {fmt_ngn(min_w)}\n"
                    f"Available: {fmt_ngn(available)}",
                    reply_markup=back_menu())
        return
    ses = session(uid)
    ses["withdraw_amount"] = available
    await _edit(chat_id, msg_id,
                f"💵 <b>Confirm withdrawal</b>\n\n"
                f"Amount: <b>{fmt_ngn(available)}</b>\n"
                f"Processing: up to 30 minutes (manual)\n\n"
                f"This reserves your credit and requests a payout from admin.\n\n"
                f"You can still cancel by /cancel — funds return to your "
                f"referral pool.\n\nContinue?",
                reply_markup=inline_kb([
                    [ibtn("✅ Confirm", "ref:withdraw_confirm")],
                    [ibtn("❌ Cancel", "menu:referral")],
                ]))

async def confirm_referral_withdrawal(chat_id: int, msg_id: int, uid: int,
                                       st: dict, u: dict) -> None:
    ses = session(uid)
    amount = float(ses.pop("withdraw_amount", 0) or 0)
    if amount <= 0:
        await _edit(chat_id, msg_id, "❌ Nothing to withdraw.",
                    reply_markup=back_menu())
        return
    r = ensure_referral(u)
    available = referral_available(u)
    if amount > available:
        await _edit(chat_id, msg_id, "❌ Amount changed. Try again.",
                    reply_markup=back_menu())
        return
    wid = "REF" + str(uuid.uuid4())[:8].upper()
    r["reserved_ngn"] = float(r.get("reserved_ngn") or 0) + amount
    r["events"].append({
        "ts": time.time(), "kind": "withdrawal_requested",
        "amount_ngn": amount, "withdrawal_id": wid, "status": "pending",
    })
    save_state()
    oid = int(settings(st).get("owner_id") or 0)
    if oid:
        try:
            await bot.send_message(
                oid,
                f"🏦 <b>Referral withdrawal request</b>\n\n"
                f"User: <code>{u['uid']}</code> "
                f"({esc(u.get('name') or '—')})\n"
                f"Amount: <b>{fmt_ngn(amount)}</b>\n"
                f"ID: <code>{wid}</code>\n\n"
                f"Pay: <code>/paid {wid}</code>\n"
                f"Fail: <code>/withdrawfail {wid} reason</code>",
                parse_mode="HTML")
        except Exception as e:
            log.warning("withdraw notify: %s", e)
    await _edit(chat_id, msg_id,
                f"✅ <b>Withdrawal submitted</b>\n\n"
                f"💵 Amount: <b>{fmt_ngn(amount)}</b>\n"
                f"📋 ID: <code>{wid}</code>\n"
                f"⏱ Processing: <b>up to 30 minutes</b>",
                reply_markup=inline_kb([
                    [ibtn("🎁 Referral", "menu:referral")],
                    [ibtn("⬅️ Main menu", "menu:main")],
                ]))

async def show_referral_history(chat_id: int, msg_id: int, st: dict,
                                 u: dict) -> None:
    r = ensure_referral(u)
    events = list((r.get("events") or []))[-20:][::-1]
    if not events:
        await _edit(chat_id, msg_id,
                    "📜 <b>Referral history</b>\n\nNo events yet.",
                    reply_markup=back_menu())
        return
    lines = []
    for e in events:
        kind = e.get("kind")
        amt = fmt_ngn(e.get("amount_ngn", 0))
        ts = ts_str(e.get("ts"))
        if kind == "referral_bonus":
            lines.append(f"🎉 {ts} · bonus · +{amt}")
        elif kind == "withdrawal_requested":
            stt = e.get("status", "pending")
            icon = "✅" if stt == "paid" else "❌" if stt == "failed" else "⏳"
            lines.append(f"{icon} {ts} · withdraw · {amt} ({stt})")
    await _edit(chat_id, msg_id,
                "📜 <b>Referral history</b>\n\n" + "\n".join(lines),
                reply_markup=inline_kb([
                    [ibtn("🎁 Referral", "menu:referral")],
                    [ibtn("⬅️ Main menu", "menu:main")],
                ]))

# ---------------------------------------------------------------------------
# Admin callbacks
# ---------------------------------------------------------------------------

async def handle_admin_cb(chat_id: int, msg_id: int, uid: int, st: dict,
                           u: dict, data: str) -> None:
    s = settings(st)
    if data == "admin:panel":
        await _edit(chat_id, msg_id, "👑 <b>Admin Panel</b>\n\nSelect an option:",
                    reply_markup=admin_menu(st))
        return
    if data == "admin:set_key":
        session(uid)["admin_state"] = "set_key"
        await _edit(chat_id, msg_id,
                    "🔑 <b>Set API Key</b>\n\nSend the key as a message.")
        return
    if data == "admin:balance":
        await _edit(chat_id, msg_id, "⏳ <b>Checking wallet…</b>")
        try:
            resp = await asyncio.to_thread(lambda: gateway(st).balance())
            await _edit(chat_id, msg_id,
                        f"💰 <b>textsms.io Wallet</b>\n\n"
                        f"Available: <b>{fmt_ngn(resp.get('balance', 0))}</b>",
                        reply_markup=admin_menu(st))
        except TextSMSError as e:
            await _edit(chat_id, msg_id,
                        f"❌ <code>{esc(e.code)}</code> — {esc(e.message)}",
                        reply_markup=admin_menu(st))
        return
    if data.startswith("admin:prices:"):
        page = int(data.split(":")[2])
        await show_price_editor(chat_id, msg_id, st, page)
        return
    if data.startswith("admin:price_set:"):
        sid = int(data.split(":", 2)[2])
        await show_price_setter(chat_id, msg_id, uid, st, sid)
        return
    if data == "admin:search_price":
        session(uid)["admin_state"] = "search_price"
        await _edit(chat_id, msg_id,
                    "🔍 <b>Search & Set Price</b>\n\nSend a keyword.",
                    reply_markup=inline_kb([[ibtn("⬅️ Admin", "admin:panel")]]))
        return
    if data == "admin:bulk_prices":
        await _edit(chat_id, msg_id,
                    "⚡ <b>Bulk Set Prices</b>\n\nChoose:",
                    reply_markup=inline_kb([
                        [ibtn("🌍 All US", "admin:bulk:country:US")],
                        [ibtn("🌍 All UK", "admin:bulk:country:UK")],
                        [ibtn("📱 All WhatsApp", "admin:bulk:name:whatsapp")],
                        [ibtn("📱 All Telegram", "admin:bulk:name:telegram")],
                        [ibtn("📱 All Instagram", "admin:bulk:name:instagram")],
                        [ibtn("📱 All TikTok", "admin:bulk:name:tiktok")],
                        [ibtn("📱 All Facebook", "admin:bulk:name:facebook")],
                        [ibtn("⬅️ Admin", "admin:panel")],
                    ]))
        return
    if data.startswith("admin:bulk:country:") or data.startswith("admin:bulk:name:"):
        parts = data.split(":")
        kind, val = parts[2], ":".join(parts[3:])
        ses = session(uid)
        ses["bulk"] = {"kind": kind, "value": val}
        ses["admin_state"] = "bulk_price"
        await _edit(chat_id, msg_id,
                    f"⚡ Send the naira price for all <b>{esc(val)}</b>:")
        return
    if data == "admin:deposits":
        await show_pending_deposits(chat_id, msg_id, st)
        return
    if data.startswith("admin:depok:"):
        await admin_confirm_deposit(chat_id, msg_id, st,
                                     data.split(":", 2)[2])
        return
    if data.startswith("admin:depno:"):
        await admin_deny_deposit(chat_id, msg_id, st,
                                  data.split(":", 2)[2])
        return
    if data == "admin:addbal":
        session(uid)["admin_state"] = "addbal"
        await _edit(chat_id, msg_id,
                    "➕ <b>Add Balance</b>\n\nSend: <code>USER_ID AMOUNT</code>")
        return
    if data == "admin:deduct":
        session(uid)["admin_state"] = "deduct"
        await _edit(chat_id, msg_id,
                    "➖ <b>Deduct Balance</b>\n\nSend: <code>USER_ID AMOUNT</code>")
        return
    if data == "admin:ban":
        session(uid)["admin_state"] = "ban"
        await _edit(chat_id, msg_id,
                    "🚫 <b>Ban / Unban</b>\n\nSend:\n"
                    "<code>BAN USER_ID</code>\n<code>UNBAN USER_ID</code>")
        return
    if data == "admin:socials":
        await show_socials_editor(chat_id, msg_id, st)
        return
    if data.startswith("admin:social_set:"):
        key = data.split(":", 2)[2]
        session(uid)["socials_key"] = key
        session(uid)["admin_state"] = "socials_set"
        labels = {
            "telegram_channel": "📢 Telegram Channel",
            "telegram_group": "💬 Telegram Group",
            "whatsapp_group": "🟢 WhatsApp Group",
            "whatsapp_channel": "🟩 WhatsApp Channel",
        }
        await _edit(chat_id, msg_id,
                    f"{labels.get(key, key)}\n\n"
                    f"Send the full URL (https://...). "
                    f"Send <code>-</code> to clear.")
        return
    if data == "admin:set_admin":
        session(uid)["admin_state"] = "set_admin_username"
        await _edit(chat_id, msg_id,
                    "📞 <b>Set Admin Username</b>\n\n"
                    "Send your handle without @.\n"
                    "Send <code>-</code> to clear.")
        return
    if data == "admin:complaints":
        await show_complaints(chat_id, msg_id, st)
        return
    if data.startswith("admin:comp_view:"):
        await show_complaint_detail(chat_id, msg_id, st,
                                     data.split(":", 2)[2])
        return
    if data.startswith("admin:comp_reply:"):
        ticket = data.split(":", 2)[2]
        session(uid)["admin_state"] = f"reply_complaint:{ticket}"
        await _edit(chat_id, msg_id,
                    f"✍️ <b>Reply to #{ticket}</b>\n\nSend your reply.")
        return
    if data.startswith("admin:comp_close:"):
        ticket = data.split(":", 2)[2]
        comps = s.setdefault("complaints", {})
        c = comps.get(ticket)
        if c:
            c["status"] = "closed"
            save_state()
        await show_complaint_detail(chat_id, msg_id, st, ticket)
        return
    if data == "admin:toggle_legacy":
        leg = s.setdefault("referral", {})
        leg["enabled"] = not leg.get("enabled")
        save_state()
        await _edit(chat_id, msg_id, "👑 <b>Admin Panel</b>",
                    reply_markup=admin_menu(st))
        return
    if data == "admin:broadcast":
        session(uid)["admin_state"] = "broadcast_text"
        session(uid)["broadcast_payload"] = {}
        await _edit(chat_id, msg_id,
                    "📢 <b>Broadcast — Step 1/2</b>\n\n"
                    "Send the text.\n\n"
                    "Or send <code>-</code> to skip text (media only).")
        return
    if data == "admin:stats":
        await show_admin_stats(chat_id, msg_id, st)
        return
    if data == "admin:toggle_maint":
        s["maintenance"] = not s.get("maintenance")
        save_state()
        await _edit(chat_id, msg_id, "👑 <b>Admin Panel</b>",
                    reply_markup=admin_menu(st))
        return

async def show_socials_editor(chat_id: int, msg_id: int, st: dict) -> None:
    soc = settings(st).get("socials") or {}
    def slot(key, label):
        v = (soc.get(key) or "").strip()
        return f"{label}: {'✅' if v else '❌'}"
    rows = [
        [ibtn(slot("telegram_channel", "📢 TG Channel"),
              "admin:social_set:telegram_channel")],
        [ibtn(slot("telegram_group", "💬 TG Group"),
              "admin:social_set:telegram_group")],
        [ibtn(slot("whatsapp_group", "🟢 WA Group"),
              "admin:social_set:whatsapp_group")],
        [ibtn(slot("whatsapp_channel", "🟩 WA Channel"),
              "admin:social_set:whatsapp_channel")],
        [ibtn("⬅️ Admin", "admin:panel")],
    ]
    await _edit(chat_id, msg_id,
                "🌐 <b>Social Links</b>\n\nTap a slot to set its URL.",
                reply_markup=inline_kb(rows))

async def show_complaints(chat_id: int, msg_id: int, st: dict) -> None:
    comps = settings(st).get("complaints") or {}
    open_items = [(k, v) for k, v in comps.items() if v.get("status") == "open"]
    if not open_items:
        await _edit(chat_id, msg_id,
                    "✍️ <b>Complaints</b>\n\n✅ No open complaints.",
                    reply_markup=inline_kb([[ibtn("⬅️ Admin", "admin:panel")]]))
        return
    open_items.sort(key=lambda kv: kv[1].get("created_ts", 0))
    rows = []
    for ticket, c in open_items[:15]:
        label = f"#{ticket} · {c.get('name','?')[:15]}"
        rows.append([ibtn(shorten(label, 55), f"admin:comp_view:{ticket}")])
    rows.append([ibtn("⬅️ Admin", "admin:panel")])
    await _edit(chat_id, msg_id,
                f"✍️ <b>Open Complaints ({len(open_items)})</b>",
                reply_markup=inline_kb(rows))

async def show_complaint_detail(chat_id: int, msg_id: int, st: dict,
                                 ticket: str) -> None:
    comps = settings(st).get("complaints") or {}
    c = comps.get(ticket)
    if not c:
        await _edit(chat_id, msg_id, "⚠️ Not found.",
                    reply_markup=inline_kb([[ibtn("⬅️ Admin", "admin:panel")]]))
        return
    replies = c.get("replies") or []
    reply_lines = "\n".join(
        f"• {ts_str(r.get('ts'))}: {esc(r.get('text'))}" for r in replies[-5:])
    if not reply_lines:
        reply_lines = "(no replies yet)"
    rows = [
        [ibtn("✍️ Reply", f"admin:comp_reply:{ticket}")],
        [ibtn("🔒 Close", f"admin:comp_close:{ticket}")],
        [ibtn("⬅️ Complaints", "admin:complaints")],
    ]
    await _edit(chat_id, msg_id,
                f"✍️ <b>Complaint #{ticket}</b>\n\n"
                f"👤 <code>{c.get('uid')}</code> "
                f"({esc(c.get('name','—'))})\n"
                f"🕐 {ts_str(c.get('created_ts'))}\n"
                f"📌 Status: <b>{c.get('status','open')}</b>\n\n"
                f"<b>Message:</b>\n{esc(c.get('text',''))}\n\n"
                f"<b>Replies:</b>\n{reply_lines}",
                reply_markup=inline_kb(rows))

async def show_admin_stats(chat_id: int, msg_id: int, st: dict) -> None:
    users = st.get("users", {})
    total = len(users)
    banned = sum(1 for x in users.values() if x.get("banned"))
    active = sum(len(x.get("active_rentals") or {}) for x in users.values())
    queued = sum(len(x.get("pending_rentals") or []) for x in users.values())
    total_rentals = sum(int(x.get("successful_rentals") or 0)
                        for x in users.values())
    today_start = time.time() - 86400
    today_rentals = 0
    today_deposits_n = 0
    today_deposits_sum = 0.0
    for x in users.values():
        for h in (x.get("history") or []):
            if float(h.get("closed_ts") or 0) >= today_start:
                today_rentals += 1
        for d in (x.get("deposits") or []):
            if (d.get("status") == "confirmed"
                    and float(d.get("created_ts") or 0) >= today_start):
                today_deposits_n += 1
                today_deposits_sum += float(d.get("amount_ngn") or 0)
    s = settings(st)
    leg = s.get("referral") or {}
    n_comp = sum(1 for c in (s.get("complaints") or {}).values()
                 if c.get("status") == "open")
    await _edit(chat_id, msg_id,
                f"📊 <b>Live Stats</b>\n\n"
                f"<b>All Time</b>\n"
                f"👥 Users: <b>{total}</b>\n"
                f"⛔ Banned: <b>{banned}</b>\n"
                f"✅ Purchases: <b>{total_rentals}</b>\n"
                f"🔄 Active: <b>{active}</b>\n"
                f"⏳ Queued: <b>{queued}</b>\n"
                f"✍️ Open complaints: <b>{n_comp}</b>\n"
                f"🎯 Referral: {'🟢 ON' if leg.get('enabled') else '🔴 OFF'}\n\n"
                f"<b>Last 24h</b>\n"
                f"✅ Purchases: <b>{today_rentals}</b>\n"
                f"💰 Deposits: <b>{today_deposits_n}</b> "
                f"({fmt_ngn(today_deposits_sum)})",
                reply_markup=admin_menu(st))

async def show_price_editor(chat_id: int, msg_id: int, st: dict,
                             page: int) -> None:
    await _edit(chat_id, msg_id, "⏳ <b>Loading…</b>")
    services = await fetch_services_cached(st)
    if not services:
        await _edit(chat_id, msg_id,
                    "⚠️ Could not load services. Check API key.",
                    reply_markup=admin_menu(st))
        return
    per = 8
    pages = max(1, (len(services) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    chunk = services[page * per:(page + 1) * per]
    rows = []
    for svc in chunk:
        sid = svc.get("id")
        p = price_in_ngn(st, sid)
        lp = fmt_ngn(p) if p else "❌"
        label = f"{shorten(svc.get('name'), 16)} {svc.get('country')} → {lp}"
        rows.append([ibtn(label, f"admin:price_set:{sid}")])
    nav = []
    if page > 0:
        nav.append(ibtn("◀ Prev", f"admin:prices:{page - 1}"))
    nav.append(ibtn(f"{page + 1}/{pages}", f"admin:prices:{page}"))
    if page < pages - 1:
        nav.append(ibtn("Next ▶", f"admin:prices:{page + 1}"))
    rows.append(nav)
    rows.append([ibtn("⬅️ Admin", "admin:panel")])
    await _edit(chat_id, msg_id, "💲 <b>Set Prices</b>",
                reply_markup=inline_kb(rows))

async def show_price_setter(chat_id: int, msg_id: int, uid: int, st: dict,
                             sid: int) -> None:
    services = await fetch_services_cached(st)
    match = next((s for s in services if s.get("id") == sid), None)
    if not match:
        await _edit(chat_id, msg_id, "⚠️ Not found.",
                    reply_markup=admin_menu(st))
        return
    provider = float(match.get("price") or 0)
    current = price_in_ngn(st, sid)
    ses = session(uid)
    ses["price_sid"] = sid
    ses["admin_state"] = "price_set"
    await _edit(chat_id, msg_id,
                f"💲 <b>{esc(match.get('name'))} · {esc(match.get('country'))}</b>\n\n"
                f"🏦 Provider: <b>{fmt_ngn(provider)}</b>\n"
                f"💵 Current: <b>{fmt_ngn(current) if current else 'not set'}</b>\n\n"
                f"Send new naira price, or <code>0</code> to hide.",
                reply_markup=inline_kb([[ibtn("⬅️ Prices", "admin:prices:0")]]))

async def show_pending_deposits(chat_id: int, msg_id: int, st: dict) -> None:
    pending = []
    for uid_str, u in st.get("users", {}).items():
        for d in (u.get("deposits") or []):
            if d.get("status") == "pending":
                age = (time.time() - float(d.get("created_ts") or 0)) / 60
                pending.append((age, uid_str, u, d))
    if not pending:
        await _edit(chat_id, msg_id,
                    "📥 <b>Pending Deposits</b>\n\n✅ None.",
                    reply_markup=admin_menu(st))
        return
    pending.sort()
    rows = []
    lines = []
    for age, uid_str, u, d in pending[:15]:
        lines.append(f"💰 <code>{esc(d['id'])}</code> · "
                     f"<b>{fmt_ngn(d.get('amount_ngn', 0))}</b> · "
                     f"user <code>{uid_str}</code> · {int(age)}m")
        rows.append([
            ibtn(f"✅ {d['id']}", f"admin:depok:{d['id']}"),
            ibtn(f"❌ {d['id']}", f"admin:depno:{d['id']}"),
        ])
    rows.append([ibtn("⬅️ Admin", "admin:panel")])
    await _edit(chat_id, msg_id,
                "📥 <b>Pending Deposits</b>\n\n" + "\n".join(lines),
                reply_markup=inline_kb(rows))

async def admin_confirm_deposit(chat_id: int, msg_id: int, st: dict,
                                 dep_id: str) -> None:
    def apply(s: dict):
        for uid_str, u in s.get("users", {}).items():
            for d in (u.get("deposits") or []):
                if d.get("id") == dep_id and d.get("status") == "pending":
                    d["status"] = "confirmed"
                    d["confirmed_ts"] = time.time()
                    add_balance(u, float(d.get("amount_ngn") or 0))
                    return int(uid_str), d, u
        return None

    result = mutate_state(apply)
    if not result:
        await _edit(chat_id, msg_id, "⚠️ Not found.",
                    reply_markup=admin_menu(st))
        return
    target_uid, d, tu = result
    try:
        await bot.send_message(
            target_uid,
            f"✅ <b>Deposit Approved!</b>\n\n"
            f"💵 Amount: <b>{fmt_ngn(d.get('amount_ngn', 0))}</b>\n"
            f"💰 New balance: <b>{fmt_ngn(balance(tu))}</b>\n\n"
            f"🚀 Buy a number now!",
            parse_mode="HTML",
            reply_markup=reply_keyboard(is_owner(target_uid, load_state())))
    except Exception as e:
        log.warning("dep confirm DM: %s", e)
    await _edit(chat_id, msg_id,
                f"✅ <b>Approved {dep_id}</b>",
                reply_markup=admin_menu(st))

async def admin_deny_deposit(chat_id: int, msg_id: int, st: dict,
                              dep_id: str) -> None:
    def apply(s: dict):
        for uid_str, u in s.get("users", {}).items():
            for d in (u.get("deposits") or []):
                if d.get("id") == dep_id and d.get("status") == "pending":
                    d["status"] = "denied"
                    d["denied_ts"] = time.time()
                    return int(uid_str), d
        return None

    result = mutate_state(apply)
    if not result:
        await _edit(chat_id, msg_id, "⚠️ Not found.",
                    reply_markup=admin_menu(st))
        return
    target_uid, d = result
    try:
        await bot.send_message(
            target_uid,
            f"❌ <b>Deposit Rejected</b>\n\n"
            f"Amount: {fmt_ngn(d.get('amount_ngn', 0))}\n\n"
            f"Contact admin with your receipt.",
            parse_mode="HTML", reply_markup=back_menu())
    except Exception as e:
        log.warning("dep deny DM: %s", e)
    await _edit(chat_id, msg_id,
                f"❌ <b>Rejected {dep_id}</b>",
                reply_markup=admin_menu(st))

# ---------------------------------------------------------------------------
# Admin text handler
# ---------------------------------------------------------------------------

async def handle_admin_text(chat_id: int, uid: int, st: dict,
                             astate: str, text: str) -> None:
    s = settings(st)

    if astate == "set_key":
        s["api_key"] = text.strip()
        save_state()
        await bot.send_message(chat_id, "✅ API key saved.",
                               reply_markup=admin_menu(st))
        return

    if astate == "set_admin_username":
        v = text.strip().lstrip("@")
        if v == "-":
            s["admin_username"] = ""
        else:
            s["admin_username"] = v
        save_state()
        await bot.send_message(
            chat_id,
            f"✅ Admin username set to "
            f"<code>{esc(v) if v != '-' else '(cleared)'}</code>.",
            parse_mode="HTML", reply_markup=admin_menu(st))
        return

    if astate == "socials_set":
        key = session(uid).get("socials_key")
        session(uid)["socials_key"] = None
        if not key:
            return
        soc = s.setdefault("socials", {})
        v = text.strip()
        if v == "-":
            soc[key] = ""
            msg = "✅ Cleared."
        elif not v.startswith("https://"):
            await bot.send_message(chat_id, "❌ URL must start with https://")
            return
        else:
            soc[key] = v
            msg = f"✅ Saved."
        save_state()
        await bot.send_message(chat_id, msg, reply_markup=admin_menu(st))
        return

    if astate == "price_set":
        sid = session(uid).pop("price_sid", None)
        if sid is None:
            return
        try:
            n = float(text.replace(",", "").replace("₦", "").strip())
            if n < 0:
                raise ValueError
        except ValueError:
            await bot.send_message(chat_id, "❌ Invalid number.")
            return
        prices = s.setdefault("service_prices", {})
        if n == 0:
            prices.pop(str(sid), None)
            msg = f"✅ Hidden."
        else:
            prices[str(sid)] = n
            msg = f"✅ Set to {fmt_ngn(n)}."
        save_state()
        await bot.send_message(chat_id, msg, reply_markup=admin_menu(st))
        return

    if astate == "bulk_price":
        info = session(uid).pop("bulk", {}) or {}
        kind, val = info.get("kind"), info.get("value")
        if not kind or not val:
            return
        try:
            price = float(text.replace(",", "").replace("₦", "").strip())
            if price <= 0:
                raise ValueError
        except ValueError:
            await bot.send_message(chat_id, "❌ Send positive number.")
            return
        services = await fetch_services_cached(st, force=True)
        prices = s.setdefault("service_prices", {})
        n = 0
        for svc in services:
            if kind == "country" and str(svc.get("country") or "").upper() \
                    == str(val).upper():
                prices[str(svc["id"])] = price
                n += 1
            elif kind == "name" and str(val).lower() in \
                    str(svc.get("name") or "").lower():
                prices[str(svc["id"])] = price
                n += 1
        save_state()
        await bot.send_message(chat_id,
                               f"✅ Set {n} services at {fmt_ngn(price)}.",
                               reply_markup=admin_menu(st))
        return

    if astate == "search_price":
        services = await fetch_services_cached(st)
        ql = text.lower()
        matches = [x for x in services
                   if ql in str(x.get("name") or "").lower()]
        if not matches:
            await bot.send_message(chat_id,
                                   f"🔍 No match for '{esc(text)}'.",
                                   parse_mode="HTML",
                                   reply_markup=admin_menu(st))
            return
        rows = []
        for svc in matches[:20]:
            sid = svc.get("id")
            p = price_in_ngn(st, sid)
            lp = fmt_ngn(p) if p else "❌"
            rows.append([ibtn(shorten(f"{svc.get('name')} · {svc.get('country')} → {lp}", 55),
                              f"admin:price_set:{sid}")])
        rows.append([ibtn("⬅️ Admin", "admin:panel")])
        await bot.send_message(chat_id,
                               f"🔍 {len(matches)} results.",
                               parse_mode="HTML",
                               reply_markup=inline_kb(rows))
        return

    if astate == "addbal":
        parts = text.split()
        try:
            if len(parts) != 2:
                raise ValueError
            target = int(parts[0])
            amount = float(parts[1])
        except ValueError:
            await bot.send_message(chat_id,
                                   "❌ Format: <code>USER_ID AMOUNT</code>",
                                   parse_mode="HTML")
            return

        def apply(sx: dict):
            tu = get_user(sx, target)
            add_balance(tu, amount)
            return tu

        tu = mutate_state(apply)
        await bot.send_message(chat_id,
                               f"✅ Added {fmt_ngn(amount)} to {target}.\n"
                               f"New balance: {fmt_ngn(balance(tu))}",
                               reply_markup=admin_menu(st))
        return

    if astate == "deduct":
        parts = text.split()
        try:
            if len(parts) != 2:
                raise ValueError
            target = int(parts[0])
            amount = float(parts[1])
        except ValueError:
            await bot.send_message(chat_id,
                                   "❌ Format: <code>USER_ID AMOUNT</code>",
                                   parse_mode="HTML")
            return

        def apply(sx: dict):
            tu = get_user(sx, target)
            charge_balance(tu, amount)
            return tu

        tu = mutate_state(apply)
        await bot.send_message(chat_id,
                               f"✅ Deducted {fmt_ngn(amount)} from {target}.\n"
                               f"New balance: {fmt_ngn(balance(tu))}",
                               reply_markup=admin_menu(st))
        return

    if astate == "ban":
        parts = text.split()
        if len(parts) < 2:
            await bot.send_message(chat_id, "❌ Format: BAN USER_ID")
            return
        action = parts[0].upper()
        try:
            target = int(parts[1])
        except ValueError:
            await bot.send_message(chat_id, "❌ Invalid ID.")
            return
        reason = " ".join(parts[2:]) or "no reason"

        def apply(sx: dict):
            tu = get_user(sx, target)
            if action == "BAN":
                tu["banned"] = True
                tu["ban_reason"] = reason
                tu["banned_ts"] = time.time()
            elif action == "UNBAN":
                tu["banned"] = False
                tu.pop("ban_reason", None)
            return tu

        mutate_state(apply)
        msg = (f"⛔ Banned {target}."
               if action == "BAN" else f"✅ Unbanned {target}.")
        await bot.send_message(chat_id, msg, reply_markup=admin_menu(st))
        return

    if astate.startswith("reply_complaint:"):
        ticket = astate.split(":", 1)[1]
        comps = s.setdefault("complaints", {})
        c = comps.get(ticket)
        if not c:
            await bot.send_message(chat_id, "⚠️ Not found.")
            return
        c.setdefault("replies", []).append({
            "ts": time.time(), "text": text, "by": "admin",
        })
        c["status"] = "answered"
        save_state()
        try:
            await bot.send_message(
                int(c["uid"]),
                f"📬 <b>Admin reply to complaint #{ticket}</b>\n\n"
                f"{esc(text)}",
                parse_mode="HTML", reply_markup=back_menu())
        except Exception as e:
            log.warning("complaint reply DM: %s", e)
        await bot.send_message(chat_id,
                               f"✅ Reply sent to {c['uid']}.",
                               reply_markup=admin_menu(st))
        return

    if astate == "broadcast_text":
        payload = session(uid).setdefault("broadcast_payload", {})
        payload["text"] = "" if text.strip() == "-" else text
        session(uid)["admin_state"] = "broadcast_media"
        await bot.send_message(
            chat_id,
            "📢 <b>Broadcast — Step 2/2</b>\n\n"
            "Send a photo or video. Or <code>-</code> for text only.")
        return

    if astate == "broadcast_media":
        if text.strip() == "-":
            ses = session(uid)
            payload = ses.get("broadcast_payload") or {}
            await _do_broadcast(chat_id, payload, media=None)
            ses["broadcast_payload"] = {}
            ses["admin_state"] = None
        return

async def _do_broadcast(chat_id: int, payload: dict,
                        media: dict | None) -> None:
    st = load_state()
    text = payload.get("text", "") or ""
    ok = fail = 0
    for uid_str in list(st.get("users", {}).keys()):
        try:
            target = int(uid_str)
            if media:
                if media.get("type") == "photo":
                    await bot.send_photo(target, media["file_id"],
                                          caption=text or "",
                                          parse_mode="HTML")
                elif media.get("type") == "video":
                    await bot.send_video(target, media["file_id"],
                                          caption=text or "",
                                          parse_mode="HTML")
            elif text:
                await bot.send_message(target, text, parse_mode="HTML")
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await bot.send_message(
        chat_id,
        f"📢 <b>Broadcast complete</b>\n\n"
        f"✅ Delivered: {ok}\n❌ Failed: {fail}",
        parse_mode="HTML", reply_markup=admin_menu(load_state()))

# ---------------------------------------------------------------------------
# Text handler
# ---------------------------------------------------------------------------

async def on_text(update: dict) -> None:
    m = update["message"]
    uid = m["from"]["id"]
    chat_id = m["chat"]["id"]
    text = (m.get("text") or "").strip()
    if not text:
        return
    st = load_state()
    u = get_user(st, uid, m["from"].get("first_name") or "")
    s = settings(st)
    admin = is_owner(uid, st)
    ses = session(uid)

    if u.get("banned") and not admin:
        await bot.send_message(
            chat_id, "⛔ <b>Your account has been suspended.</b>",
            parse_mode="HTML")
        return

    astate = ses.get("admin_state")
    if astate:
        if not admin:
            ses["admin_state"] = None
            return
        if astate not in ("broadcast_media",):
            ses["admin_state"] = None
        await handle_admin_text(chat_id, uid, st, astate, text)
        return

    if text == "📱 Buy":
        await show_services_msg(chat_id, uid, st, "", None, 0)
        return
    if text == "💰 Balance":
        ref_av = referral_available(u)
        msg = f"💰 <b>Balance:</b> {fmt_ngn(balance(u))}"
        if ref_av > 0:
            msg += (f"\n🎁 <b>Referral credit:</b> {fmt_ngn(ref_av)}"
                    f"\n🧮 <b>Total spendable:</b> "
                    f"{fmt_ngn(total_spendable(u))}")
        await bot.send_message(chat_id, msg, parse_mode="HTML",
                               reply_markup=reply_keyboard(admin))
        return
    if text == "📜 History":
        await send_history(chat_id, st, u)
        return
    if text == "🔄 Active":
        await show_active_msg(chat_id, st, u)
        return
    if text == "💳 Add Money":
        await start_deposit_flow(chat_id, 0, uid, st, u)
        return
    if text == "🌐 Channels":
        t, kb = channels_card(st)
        await bot.send_message(chat_id, t, parse_mode="HTML",
                               reply_markup=kb)
        return
    if text == "🎁 Referral":
        await show_referral_msg(chat_id, st, u)
        return
    if text == "🆘 Support":
        t, kb = support_card(st)
        await bot.send_message(chat_id, t, parse_mode="HTML",
                               reply_markup=kb)
        return
    if text == "ℹ️ Help":
        await bot.send_message(chat_id, HELP_TEXT, parse_mode="HTML",
                               reply_markup=reply_keyboard(admin))
        return
    if text == "👑 Admin" and admin:
        await bot.send_message(chat_id, "👑 <b>Admin Panel</b>",
                               parse_mode="HTML", reply_markup=admin_menu(st))
        return

    if not admin and s.get("maintenance"):
        await bot.send_message(chat_id, "🚧 Maintenance. Try again later.")
        return

    state = ses.get("state")
    if state == "await_svc_query":
        ses["state"] = None
        q_val = "" if text == "*" else text
        ses["svc_query"] = q_val
        await show_services_search(chat_id, st, q_val)
        return
    if state == "complaint_text":
        ses["state"] = None
        ticket = None
        def apply(sx: dict):
            nonlocal ticket
            ticket = next_complaint_id(sx)
            save_complaint(sx, ticket, uid, u.get("name") or "—",
                           text, 0)
            return ticket
        mutate_state(apply)
        oid = int(s.get("owner_id") or 0)
        if oid:
            try:
                sent = await bot.send_message(
                    oid,
                    f"✍️ <b>New Complaint #{ticket}</b>\n\n"
                    f"👤 <code>{uid}</code> ({esc(u.get('name') or '—')})\n"
                    f"🕐 {ts_str(time.time())}\n\n"
                    f"<b>Message:</b>\n{esc(text)}",
                    parse_mode="HTML",
                    reply_markup=inline_kb([
                        [ibtn("✍️ Reply", f"admin:comp_reply:{ticket}")],
                        [ibtn("🔒 Close", f"admin:comp_close:{ticket}")],
                    ]))
                def apply2(sx: dict):
                    comps = settings(sx).setdefault("complaints", {})
                    c = comps.get(str(ticket))
                    if c:
                        c["admin_msg_id"] = sent.get("message_id", 0)
                mutate_state(apply2)
            except Exception as e:
                log.warning("complaint DM: %s", e)
        await bot.send_message(
            chat_id,
            f"✅ <b>Complaint submitted</b>\n\n"
            f"🎫 Ticket: <code>#{ticket}</code>\n\n"
            f"Admin has been notified. You'll get a reply here.",
            parse_mode="HTML", reply_markup=reply_keyboard(admin))
        return
    if state == "dep_amount":
        try:
            amount = float(text.replace(",", "").replace("₦", "").strip())
        except ValueError:
            await bot.send_message(
                chat_id, "❌ Send a number like <code>4000</code>",
                parse_mode="HTML")
            return
        cfg = settings(st).get("bank") or {}
        min_d = float(cfg.get("min_deposit_ngn") or 1000)
        if amount < min_d:
            await bot.send_message(chat_id, f"❌ Minimum {fmt_ngn(min_d)}.")
            return
        if amount > 5_000_000:
            await bot.send_message(chat_id, "❌ Max ₦5,000,000.")
            return
        ses["dep_amount"] = amount
        ses["state"] = "dep_paid_prompt"
        await bot.send_message(
            chat_id,
            f"✅ Amount: <b>{fmt_ngn(amount)}</b>\n\n"
            f"Have you transferred {fmt_ngn(amount)} to the account shown?\n\n"
            f"Tap <b>✅ I have paid</b> once done.",
            parse_mode="HTML",
            reply_markup=inline_kb([
                [ibtn("✅ I have paid", "topup:confirm_paid")],
                [ibtn("❌ Cancel", "menu:main")],
            ]))
        return
    if not state:
        await bot.send_message(chat_id, "Use /start to open the menu.",
                               reply_markup=reply_keyboard(admin))
        return
    ses["state"] = None
    await bot.send_message(chat_id, "Session reset. Use /start.",
                           reply_markup=reply_keyboard(admin))

async def show_services_msg(chat_id: int, uid: int, st: dict, query: str,
                             country: str | None, page: int) -> None:
    await bot.send_message(chat_id, "⏳ <b>Loading services…</b>",
                           parse_mode="HTML")
    services = await fetch_services_cached(st)
    if country:
        services = [s for s in services
                    if str(s.get("country") or "").upper() == country.upper()]
    if query:
        ql = query.lower()
        services = [s for s in services
                    if ql in str(s.get("name") or "").lower()]
    sellable = [s for s in services if price_in_ngn(st, s.get("id")) is not None]
    admin = is_owner(uid, st)
    if not sellable:
        await bot.send_message(chat_id, "📱 <b>No services available</b>",
                               parse_mode="HTML",
                               reply_markup=reply_keyboard(admin))
        return
    await bot.send_message(
        chat_id,
        f"📱 <b>Available Services</b>\nTotal: <b>{len(sellable)}</b>\n\n"
        "Tap to buy 👇",
        parse_mode="HTML",
        reply_markup=services_keyboard(st, sellable, page, 6, country))

async def show_services_search(chat_id: int, st: dict, query: str) -> None:
    services = await fetch_services_cached(st)
    ql = query.lower()
    if ql:
        services = [s for s in services
                    if ql in str(s.get("name") or "").lower()]
    sellable = [s for s in services if price_in_ngn(st, s.get("id")) is not None]
    if not sellable:
        await bot.send_message(chat_id, "🔍 No matching services.",
                               reply_markup=back_menu())
        return
    await bot.send_message(
        chat_id,
        f"🔍 <b>{len(sellable)} results</b> for: {esc(query or 'all')}",
        parse_mode="HTML",
        reply_markup=services_keyboard(st, sellable, 0, 6, None))

async def show_active_msg(chat_id: int, st: dict, u: dict) -> None:
    rentals = u.get("active_rentals") or {}
    admin = is_owner(u["uid"], st)
    if not rentals:
        await bot.send_message(chat_id,
                               "🔄 <b>My Numbers</b>\n\nNo open numbers.",
                               parse_mode="HTML",
                               reply_markup=reply_keyboard(admin))
        return
    rows = []
    lines = []
    for rid_str, r in rentals.items():
        try:
            rid = int(rid_str)
        except ValueError:
            continue
        code = (r.get("code") or "").strip()
        status = "✅ Code received" if code else "⏳ Waiting"
        lines.append(f"• <b>{esc(r.get('service'))}</b> · "
                     f"<code>{esc(r.get('number'))}</code> · {status}")
        rows.append([ibtn(f"👁 {shorten(str(r.get('service')), 20)}",
                          f"act:view:{rid}")])
    await bot.send_message(chat_id,
                           "🔄 <b>My Numbers</b>\n\n" + "\n".join(lines),
                           parse_mode="HTML", reply_markup=inline_kb(rows))

async def on_photo(update: dict) -> None:
    m = update["message"]
    uid = m["from"]["id"]
    chat_id = m["chat"]["id"]
    tg_name = m["from"].get("first_name") or ""
    st = load_state()
    u = get_user(st, uid, tg_name)
    admin = is_owner(uid, st)
    ses = session(uid)

    if u.get("banned") and not admin:
        await bot.send_message(chat_id, "⛔ Account suspended.")
        return

    if admin and ses.get("admin_state") == "broadcast_media":
        photos = m.get("photo") or []
        if not photos:
            return
        payload = ses.get("broadcast_payload") or {}
        media = {"type": "photo", "file_id": photos[-1]["file_id"]}
        await _do_broadcast(chat_id, payload, media=media)
        ses["broadcast_payload"] = {}
        ses["admin_state"] = None
        return

    if ses.get("state") != "dep_screenshot":
        await bot.send_message(
            chat_id,
            "📸 I only accept photos during the deposit step.")
        return
    amount = float(ses.pop("dep_amount", 0) or 0)
    if amount <= 0:
        ses["state"] = None
        await bot.send_message(chat_id, "❌ Something went wrong.")
        return
    photos = m.get("photo") or []
    if not photos:
        return
    photo = photos[-1]
    payer_name = (u.get("name") or tg_name or "—")
    dep_id = str(uuid.uuid4())[:10].upper()
    dep = {
        "id": dep_id,
        "user_id": uid,
        "amount_ngn": amount,
        "payer_name": payer_name,
        "photo_file_id": photo["file_id"],
        "status": "pending",
        "created_ts": time.time(),
    }
    u.setdefault("deposits", []).append(dep)
    ses["state"] = None
    save_state()
    oid = int(settings(st).get("owner_id") or 0)
    if oid:
        try:
            await bot.send_photo(
                oid,
                photo=photo["file_id"],
                caption=(
                    f"💰 <b>New Deposit</b>\n\n"
                    f"User: <code>{u['uid']}</code> ({esc(payer_name)})\n"
                    f"Amount: <b>{fmt_ngn(amount)}</b>\n"
                    f"ID: <code>{dep_id}</code>"),
                parse_mode="HTML",
                reply_markup=inline_kb([
                    [ibtn("✅ Approve", f"admin:depok:{dep_id}"),
                     ibtn("❌ Reject", f"admin:depno:{dep_id}")],
                ]))
        except Exception as e:
            log.warning("dep notify: %s", e)
    await bot.send_message(
        chat_id,
        f"⏳ <b>Wait for Approval</b>\n\n"
        f"💵 Amount: <b>{fmt_ngn(amount)}</b>\n"
        f"🆔 ID: <code>{dep_id}</code>\n\n"
        f"Admin will verify and credit you shortly.",
        parse_mode="HTML", reply_markup=reply_keyboard(admin))

async def on_video(update: dict) -> None:
    m = update["message"]
    uid = m["from"]["id"]
    chat_id = m["chat"]["id"]
    st = load_state()
    ses = session(uid)
    if not is_owner(uid, st):
        return
    if ses.get("admin_state") == "broadcast_media":
        video = m.get("video") or {}
        if not video.get("file_id"):
            return
        payload = ses.get("broadcast_payload") or {}
        media = {"type": "video", "file_id": video["file_id"]}
        await _do_broadcast(chat_id, payload, media=media)
        ses["broadcast_payload"] = {}
        ses["admin_state"] = None

# ---------------------------------------------------------------------------
# Background: queue worker
# ---------------------------------------------------------------------------

async def queue_worker() -> None:
    while True:
        await asyncio.sleep(QUEUE_RETRY_FAST_SECONDS)
        try:
            st = load_state()
            s = settings(st)
            max_min = QUEUE_MAX_MINUTES
            queued = []
            for uid_str, u in st.get("users", {}).items():
                try:
                    uid = int(uid_str)
                except ValueError:
                    continue
                for p in (u.get("pending_rentals") or []):
                    if p.get("state") == "queued":
                        queued.append((uid, u, p))
            if not queued:
                await asyncio.sleep(QUEUE_RETRY_SECONDS
                                    - QUEUE_RETRY_FAST_SECONDS
                                    if QUEUE_RETRY_SECONDS
                                    > QUEUE_RETRY_FAST_SECONDS else 0)
                continue
            my_balance = await get_provider_balance(st, force=True)
            buffer_ngn = float(s.get("safety_buffer_ngn") or 0)
            queued.sort(key=lambda x: x[2].get("queued_ts", 0))

            for uid, u, p in queued:
                try:
                    required = (float(p.get("provider_price_ngn") or 0)
                                + buffer_ngn)
                    age_min = (time.time()
                               - float(p.get("queued_ts") or 0)) / 60
                    if age_min >= max_min:
                        price = float(p.get("price_ngn") or 0)
                        refund_balance(u, price, source=p.get("charge_source"))
                        u["pending_rentals"] = [
                            x for x in u.get("pending_rentals", [])
                            if x["id"] != p["id"]]
                        save_state()
                        try:
                            await bot.send_message(
                                uid,
                                f"⚠️ <b>Could Not Complete</b>\n\n"
                                f"{esc(p.get('service'))} · "
                                f"{esc(p.get('country'))}\n\n"
                                f"Supplier issues. Refunded "
                                f"<b>{fmt_ngn(price)}</b>.",
                                parse_mode="HTML", reply_markup=back_menu())
                        except Exception:
                            pass
                        continue
                    if my_balance < required:
                        continue
                    match = {"name": p.get("service"),
                             "country_name": p.get("country"),
                             "price": p.get("provider_price_ngn")}
                    ok = await _execute_rent(
                        None, None, st, u, int(p["service_id"]), match,
                        float(p.get("price_ngn") or 0),
                        float(p.get("provider_price_ngn") or 0),
                        from_queue=True)
                    if ok:
                        u["pending_rentals"] = [
                            x for x in u["pending_rentals"]
                            if x["id"] != p["id"]]
                        save_state()
                        my_balance = await get_provider_balance(st,
                                                                force=True)
                    else:
                        p["attempts"] = int(p.get("attempts", 0)) + 1
                        save_state()
                except Exception as e:
                    log.warning("queue item %s/%s failed: %s",
                                uid, p.get("id"), e)
                    continue
        except Exception as e:
            log.exception("queue worker: %s", e)

async def balance_watcher() -> None:
    while True:
        await asyncio.sleep(BALANCE_WATCH_SECONDS)
        try:
            st = load_state()
            has_queue = False
            min_required = None
            for u in st.get("users", {}).values():
                for p in (u.get("pending_rentals") or []):
                    if p.get("state") == "queued":
                        has_queue = True
                        req = float(p.get("provider_price_ngn") or 0) + \
                            float(settings(st).get("safety_buffer_ngn") or 0)
                        if min_required is None or req < min_required:
                            min_required = req
                        break
                if has_queue:
                    break
            if not has_queue:
                continue
            my_balance = await get_provider_balance(st, force=True)
            if my_balance < (min_required or 0):
                continue
            log.info("balance restored — draining queue")
            await _drain_queue_once()
        except Exception as e:
            log.warning("balance watcher: %s", e)

async def _drain_queue_once() -> None:
    st = load_state()
    s = settings(st)
    my_balance = await get_provider_balance(st, force=True)
    buffer_ngn = float(s.get("safety_buffer_ngn") or 0)
    queued = []
    for uid_str, u in st.get("users", {}).items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        for p in (u.get("pending_rentals") or []):
            if p.get("state") == "queued":
                queued.append((uid, u, p))
    queued.sort(key=lambda x: x[2].get("queued_ts", 0))
    for uid, u, p in queued:
        try:
            required = float(p.get("provider_price_ngn") or 0) + buffer_ngn
            if my_balance < required:
                continue
            match = {"name": p.get("service"),
                     "country_name": p.get("country"),
                     "price": p.get("provider_price_ngn")}
            ok = await _execute_rent(
                None, None, st, u, int(p["service_id"]), match,
                float(p.get("price_ngn") or 0),
                float(p.get("provider_price_ngn") or 0),
                from_queue=True)
            if ok:
                u["pending_rentals"] = [
                    x for x in u["pending_rentals"] if x["id"] != p["id"]]
                save_state()
                my_balance = await get_provider_balance(st, force=True)
            else:
                p["attempts"] = int(p.get("attempts", 0)) + 1
                save_state()
        except Exception as e:
            log.warning("drain item %s/%s failed: %s", uid, p.get("id"), e)
            continue

# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

_COMMANDS = {
    "/start": cmd_start,
    "/help": cmd_help,
    "/cancel": cmd_cancel,
    "/balance": cmd_balance,
    "/history": cmd_history,
    "/admin": cmd_admin,
    "/paid": cmd_paid,
    "/withdrawfail": cmd_withdrawfail,
    "/reply": cmd_reply,
}

async def dispatch_command(update: dict) -> None:
    m = update["message"]
    text = (m.get("text") or "").strip()
    if not text.startswith("/"):
        return
    cmd = text.split()[0].split("@")[0].lower()
    handler = _COMMANDS.get(cmd)
    if not handler:
        return
    try:
        await handler(update)
    except Exception as e:
        log.exception("command %s failed: %s", cmd, e)

async def dispatch_message(update: dict) -> None:
    m = update["message"]
    if m.get("photo"):
        await on_photo(update)
        return
    if m.get("video"):
        await on_video(update)
        return
    if m.get("successful_payment"):
        return
    if m.get("text", "").startswith("/"):
        await dispatch_command(update)
        return
    await on_text(update)

# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

bot: TelegramBot | None = None

async def bootstrap_commands() -> None:
    try:
        await bot.set_my_name(BOT_DISPLAY_NAME)
    except Exception as e:
        log.warning("set name: %s", e)
    try:
        await bot.set_my_commands([
            {"command": "start", "description": "Main menu"},
            {"command": "help", "description": "How to use the bot"},
            {"command": "balance", "description": "My balance"},
            {"command": "history", "description": "My past numbers"},
            {"command": "cancel", "description": "Cancel current input"},
            {"command": "admin", "description": "Admin panel (owner)"},
        ])
    except Exception as e:
        log.warning("set commands: %s", e)

async def init_handlers() -> None:
    def is_message(u): return "message" in u
    def is_callback(u): return "callback_query" in u
    bot.add_handler(is_callback, on_callback)
    bot.add_handler(is_message, dispatch_message)

async def heartbeat() -> None:
    while True:
        await asyncio.sleep(300)
        try:
            st = load_state()
            n_users = len(st.get("users", {}))
            n_active = sum(len(u.get("active_rentals") or {})
                           for u in st["users"].values())
            n_queued = sum(len(u.get("pending_rentals") or [])
                           for u in st["users"].values())
            log.info("heartbeat: %d users, %d active, %d queued",
                     n_users, n_active, n_queued)
        except Exception as e:
            log.warning("heartbeat: %s", e)

async def main() -> None:
    global bot
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN env var is required.")
    bot = TelegramBot(BOT_TOKEN)
    try:
        me = await bot.get_me()
        log.info("Bot @%s (%s) starting", me.get("username"), me.get("id"))
    except Exception as e:
        log.error("getMe failed: %s — check BOT_TOKEN", e)
        raise

    await bootstrap_commands()
    await init_handlers()
    asyncio.create_task(resume_all_pollers())
    asyncio.create_task(queue_worker())
    asyncio.create_task(balance_watcher())
    asyncio.create_task(heartbeat())

    await bot.poll_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    except Exception as e:
        log.exception("fatal: %s", e)
        raise
