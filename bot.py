# language: Python 3.11, file: bot.py
import os, re, time, base64, random, asyncio, logging
from dataclasses import dataclass, field
from typing import Optional, Literal

import aiohttp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, filters, ContextTypes)
from playwright.async_api import async_playwright, Browser, Page

load_dotenv()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
CHANNEL_ID    = os.getenv("CHANNEL_ID", "@AADHARSAHAYAK")
CHANNEL_URL   = os.getenv("CHANNEL_URL", "https://t.me/AADHARSAHAYAK")
ADMIN_ID      = int(os.getenv("ADMIN_ID", "7197906650"))
CAPSOLVER_KEY = os.getenv("CAPSOLVER_KEY", "")
SESSION_TTL   = int(os.getenv("SESSION_TTL", "300"))

if not BOT_TOKEN:     raise SystemExit("BOT_TOKEN missing")
if not CAPSOLVER_KEY: raise SystemExit("CAPSOLVER_KEY missing")

BRAND = "AADHAAR SAHAYAK"
T = {
"gate": (f"✦ <b>{BRAND}</b> ✦\n━━━━━━━━━━━━━━━━━━\n\n"
         "Access is restricted to channel members.\n\n"
         "→ Tap <b>Join Channel</b>\n→ Then tap <b>Retry</b> to unlock."),
"gate_still": "✖ <b>Access denied.</b>\n\nJoin the channel, then hit <b>Retry</b>.",
"menu": (f"✦ <b>{BRAND}</b> ✦\n━━━━━━━━━━━━━━━━━━\n\n"
         "Select the document type to begin.\n\n"
         "<i>Each request runs on an isolated, rotated connection.</i>"),
"ask_aadhaar": "▸ <b>Step 1 of 3</b> — Aadhaar Number\n\nSend your <b>12-digit Aadhaar number</b>.",
"ask_eid":     "▸ <b>Step 1 of 3</b> — EID\n\nSend your <b>28-digit Enrolment ID</b>.",
"bad_aadhaar": "✖ Invalid Aadhaar number. Must be 12 digits.",
"bad_eid":     "✖ Invalid EID. Must be 28 digits.",
"working":     "◐ Securing a clean route and submitting…\n<i>Hold tight.</i>",
"ask_otp1":    "▸ <b>Step 2 of 3</b> — Verification OTP\n\nSend the <b>OTP</b> you received on your Aadhaar-linked mobile.",
"ask_otp2":    "▸ <b>Step 3 of 3</b> — Download OTP\n\nA second OTP was triggered. Send it to receive your PDF.",
"fail":        "✖ <b>Request failed.</b>\n\nRoute blocked or session rejected.\nTap <b>Retry</b> below — the bot rotates to a fresh connection.",
"sending_pdf": "◐ Generating PDF… sending in a moment.",
"done":        "✔ <b>Done.</b> Your document is above.\nUse /start for another request.",
"cancelled":   "✖ Request cancelled. Use /start to begin again.",
}

_rng = random.SystemRandom()

def _load_proxies() -> list[str]:
    env = os.getenv("PROXY_LIST", "").strip()
    if env:
        return [p.strip() for p in env.split(",") if p.strip()]
    path = os.path.join(os.path.dirname(__file__), "proxies.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    return []

PROXIES = _load_proxies()
_bad: set[str] = set()

def pick_proxy() -> tuple[Optional[dict], Optional[str]]:
    live = [p for p in PROXIES if p not in _bad]
    if not live:
        _bad.clear(); live = PROXIES
    if not live:
        return None, None
    raw = _rng.choice(live)
    if "@" in raw:
        scheme, rest = raw.split("://", 1) if "://" in raw else ("http", raw)
        creds, hostport = rest.rsplit("@", 1)
        user, _, pwd = creds.partition(":")
        return {"server": f"{scheme}://{hostport}", "username": user, "password": pwd}, raw
    return {"server": raw if "://" in raw else f"http://{raw}"}, raw

def mark_bad(raw: Optional[str]) -> None:
    if raw:
        _bad.add(raw); log.warning("proxy dead: %s", raw)

async def proxy_health(conc: int = 40, timeout: float = 6.0) -> int:
    if not PROXIES: return 0
    async def one(s, raw):
        try:
            async with s.get("https://api.ipify.org?format=json", proxy=raw,
                             timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return raw, r.status == 200
        except Exception:
            return raw, False
    conn = aiohttp.TCPConnector(limit=conc, ssl=False)
    async with aiohttp.ClientSession(connector=conn) as s:
        res = await asyncio.gather(*[one(s, p) for p in PROXIES])
    for p, ok in res:
        if not ok: _bad.add(p)
    log.info("proxy health: %d live / %d total", len(PROXIES) - len(_bad), len(PROXIES))
    return len(PROXIES) - len(_bad)

async def solve_captcha(session: aiohttp.ClientSession, image_b64: str) -> str:
    payload = {"clientKey": CAPSOLVER_KEY,
               "task": {"type": "ImageToTextTask", "body": image_b64, "module": "common"}}
    async with session.post("https://api.capsolver.com/createTask", json=payload) as r:
        d = await r.json()
    if d.get("errorId"): raise RuntimeError(d.get("errorDescription") or "createTask failed")
    tid = d["taskId"]
    for _ in range(40):
        await asyncio.sleep(3)
        async with session.post("https://api.capsolver.com/getTaskResult",
                                json={"clientKey": CAPSOLVER_KEY, "taskId": tid}) as r:
            res = await r.json()
        if res.get("errorId"):
            raise RuntimeError(res.get("errorDescription") or "getTaskResult failed")
        if res.get("status") == "ready":
            sol = res["solution"]
            txt = (sol.get("text") or sol.get("answer") or "").strip()
            if txt: return txt
            raise RuntimeError(f"empty solution {sol}")
    raise RuntimeError("captcha timeout")

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]
BLOCK = {"image", "media", "font"}

class Engine:
    def __init__(self): self._pw = None; self.browser: Optional[Browser] = None
    async def start(self):
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage", "--disable-gpu"])
        await proxy_health()
    async def stop(self):
        if self.browser: await self.browser.close()
        if self._pw:      await self._pw.stop()
    async def new_session(self):
        last = None
        for _ in range(4):
            pxy, raw = pick_proxy(); ctx = None
            try:
                ctx = await self.browser.new_context(
                    proxy=pxy, user_agent=_rng.choice(UA_POOL),
                    viewport={"width": 1366, "height": 768},
                    locale="en-IN", timezone_id="Asia/Kolkata",
                    ignore_https_errors=True)
                await ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
                page = await ctx.new_page()
                async def _route(route):
                    try:
                        if route.request.resource_type in BLOCK: await route.abort()
                        else: await route.continue_()
                    except Exception: pass
                await page.route("**/*", _route)
                return ctx, page, raw, None
            except Exception as e:
                last = e; mark_bad(raw)
                if ctx:
                    try: await ctx.close()
                    except Exception: pass
        return None, None, None, last or RuntimeError("no session")

AADHAAR_URL = "https://myaadhaar.uidai.gov.in/genuineaadhaar/copyofAaadhaarNumber"
EID_URL     = "https://myaadhaar.uidai.gov.in/genuineaadhaar/copyofAaadhaarNumber"
NAV_TIMEOUT = 25000

SEL = {
"aadhaar_input": "input[formcontrolname='aadhaarNumber'], input#aadhaarNumber, input[placeholder*='Aadhaar' i]",
"eid_input":     "input[formcontrolname='eid'], input#eid, input[placeholder*='EID' i], input[placeholder*='Enrolment' i]",
"captcha_img":   "img.captcha-img, img[src*='captcha'], img#captchaImage",
"captcha_input": "input[formcontrolname='captcha'], input#captcha, input[placeholder*='captcha' i]",
"submit":        "button[type='submit'], button:has-text('Send OTP'), button:has-text('Next')",
"otp_input":     "input[formcontrolname='otp'], input#otp, input[placeholder*='OTP' i]",
"otp_submit":    "button:has-text('Verify'), button:has-text('Submit'), button[type='submit']",
"eid_radio":     "label:has-text('EID'), input[value='EID'], mat-radio-button:has-text('EID')",
"download":      "button:has-text('Download'), a:has-text('Download')",
}

async def uidai_open(page: Page, kind: str):
    url = AADHAAR_URL if kind == "aadhaar" else EID_URL
    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    await page.wait_for_selector(SEL["captcha_img"], timeout=15000)
    if kind == "eid":
        try: await page.click(SEL["eid_radio"], timeout=3000)
        except Exception: pass

async def uidai_submit_ident(page: Page, http: aiohttp.ClientSession, kind: str, ident: str):
    sel = SEL["aadhaar_input"] if kind == "aadhaar" else SEL["eid_input"]
    await page.fill(sel, ident)
    img = await page.query_selector(SEL["captcha_img"])
    if not img: raise RuntimeError("captcha image not found")
    b64 = base64.b64encode(await img.screenshot()).decode()
    await page.fill(SEL["captcha_input"], await solve_captcha(http, b64))
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=NAV_TIMEOUT):
        await page.click(SEL["submit"])
    await page.wait_for_selector(SEL["otp_input"], timeout=20000)

async def uidai_submit_otp(page: Page, otp: str):
    await page.fill(SEL["otp_input"], otp)
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=NAV_TIMEOUT):
        await page.click(SEL["otp_submit"])

async def uidai_download(page: Page, timeout: int = 30000) -> bytes:
    async with page.expect_download(timeout=timeout) as dl:
        await page.click(SEL["download"])
    d = await dl.value
    path = await d.path()
    with open(path, "rb") as f: return f.read()

@dataclass
class Sess:
    user_id: int
    kind: Literal["aadhaar", "eid"]
    stage: Literal["awaiting_id", "awaiting_otp1", "awaiting_otp2"]
    ident: str = ""
    ctx: object = None
    page: object = None
    created: float = field(default_factory=time.time)

SESSIONS: dict[int, Sess] = {}

def gate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Join Channel", url=CHANNEL_URL)],
        [InlineKeyboardButton("🔁 Retry", callback_data="gate:retry")],
    ])

def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆔 Aadhaar Number", callback_data="start:aadhaar")],
        [InlineKeyboardButton("🪪 EID",            callback_data="start:eid")],
    ])

def retry_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Retry", callback_data="gate:retry")]])

async def is_member(ctx: ContextTypes.DEFAULT_TYPE, uid: int) -> bool:
    try:
        m = await ctx.bot.get_chat_member(CHANNEL_ID, uid)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False

def eng(ctx): return ctx.application.bot_data["engine"]
def http(ctx): return ctx.application.bot_data["http"]
def digits(s): return re.sub(r"\D", "", s or "")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await is_member(ctx, update.effective_user.id):
        await update.effective_message.reply_text(T["menu"], parse_mode="HTML")
        await update.effective_message.reply_text("Choose a document type 👇", reply_markup=menu_kb())
    else:
        await update.effective_message.reply_text(T["gate"], parse_mode="HTML", reply_markup=gate_kb())

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = SESSIONS.pop(update.effective_user.id, None)
    if s and s.ctx:
        try: await s.ctx.close()
        except Exception: pass
    await update.effective_message.reply_text(T["cancelled"])

async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "gate:retry":
        if not await is_member(ctx, q.from_user.id):
            await q.edit_message_text(T["gate_still"], parse_mode="HTML", reply_markup=gate_kb())
            return
        await q.edit_message_text(T["menu"], parse_mode="HTML")
        await q.message.reply_text("Choose a document type 👇", reply_markup=menu_kb())
    elif data.startswith("start:"):
        kind = data.split(":", 1)[1]
        SESSIONS[q.from_user.id] = Sess(user_id=q.from_user.id, kind=kind, stage="awaiting_id")
        ask = T["ask_aadhaar"] if kind == "aadhaar" else T["ask_eid"]
        await q.edit_message_text(ask, parse_mode="HTML")

async def _fail(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int, e: Exception):
    s = SESSIONS.pop(uid, None)
    if s and s.ctx:
        try: await s.ctx.close()
        except Exception: pass
    try:
        await update.effective_message.reply_text(T["fail"], parse_mode="HTML", reply_markup=retry_kb())
    except Exception: pass
    try: await ctx.bot.send_message(ADMIN_ID, f"[fail] uid={uid} err={e}")
    except Exception: pass

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg = update.effective_message
    s = SESSIONS.get(uid)
    if not s: return

    if s.stage == "awaiting_id":
        d = digits(msg.text)
        if s.kind == "aadhaar" and len(d) != 12:
            await msg.reply_text(T["bad_aadhaar"]); return
        if s.kind == "eid" and len(d) != 28:
            await msg.reply_text(T["bad_eid"]); return
        s.ident = d; s.stage = "awaiting_otp1"
        await msg.reply_text(T["working"], parse_mode="HTML")
        await msg.chat.send_action(ChatAction.TYPING)
        c_, page, raw, err = await eng(ctx).new_session()
        if err:
            await _fail(update, ctx, uid, err); return
        s.ctx, s.page = c_, page
        try:
            await uidai_open(page, s.kind)
            await uidai_submit_ident(page, http(ctx), s.kind, s.ident)
        except Exception as e:
            await _fail(update, ctx, uid, e); return
        await msg.reply_text(T["ask_otp1"], parse_mode="HTML")
        return

    if s.stage == "awaiting_otp1":
        otp = digits(msg.text)
        if len(otp) < 4:
            await msg.reply_text("✖ OTP looks wrong. Send it again."); return
        try:
            await uidai_submit_otp(s.page, otp)
        except Exception as e:
            await _fail(update, ctx, uid, e); return
        s.stage = "awaiting_otp2"
        await msg.reply_text(T["ask_otp2"], parse_mode="HTML")
        return

    if s.stage == "awaiting_otp2":
        otp = digits(msg.text)
        if len(otp) < 4:
            await msg.reply_text("✖ OTP looks wrong. Send it again."); return
        await msg.reply_text(T["sending_pdf"], parse_mode="HTML")
        await msg.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        try:
            await uidai_submit_otp(s.page, otp)
            pdf = await uidai_download(s.page)
        except Exception as e:
            await _fail(update, ctx, uid, e); return
        SESSIONS.pop(uid, None)
        if s.ctx:
            try: await s.ctx.close()
            except Exception: pass
        fname = f"{s.kind.upper()}_{s.ident[-4:]}.pdf"
        await msg.reply_document(pdf, filename=fname, caption=T["done"], parse_mode="HTML")
        return

async def post_init(app: Application):
    app.bot_data["engine"] = Engine()
    await app.bot_data["engine"].start()
    app.bot_data["http"] = aiohttp.ClientSession()
    log.info("engine ready")

async def post_shutdown(app: Application):
    try: await app.bot_data["engine"].stop()
    except Exception: pass
    try: await app.bot_data["http"].close()
    except Exception: pass

def main():
    app = (Application.builder().token(BOT_TOKEN)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
