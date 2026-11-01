# language: Python 3.11, file: bot.py
# flow: mobile → find-uid → aadhaar → copyofAadhaarNumber → PDF
# 3 OTPs, 3 captchas, 2 browser sessions
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
SESSION_TTL   = int(os.getenv("SESSION_TTL", "600"))

if not BOT_TOKEN:     raise SystemExit("BOT_TOKEN missing")
if not CAPSOLVER_KEY: raise SystemExit("CAPSOLVER_KEY missing")

# ─────────────────────────── TEXT ─────────────────────────────
BRAND = "AADHAAR SAHAYAK"
T = {
"gate": (f"✦ <b>{BRAND}</b> ✦\n━━━━━━━━━━━━━━━━━━\n\n"
         "Access is restricted to channel members.\n\n"
         "→ Tap <b>Join Channel</b>\n→ Then tap <b>Retry</b> to unlock."),
"gate_still": "✖ <b>Access denied.</b>\n\nJoin the channel, then hit <b>Retry</b>.",
"menu": (f"✦ <b>{BRAND}</b> ✦\n━━━━━━━━━━━━━━━━━━\n\n"
         "Fetch your Aadhaar PDF using your registered mobile number.\n\n"
         "<i>Three OTPs will be requested by UIDAI. Have your phone ready.</i>"),
"ask_mobile": ("▸ <b>Step 1 of 4</b> — Mobile Number\n\n"
               "Send your <b>10-digit mobile number</b> linked to Aadhaar."),
"bad_mobile": "✖ Invalid mobile number. Must be 10 digits.",
"working_find": "◐ Locating your Aadhaar record…\n<i>Solving captcha.</i>",
"ask_otp1": ("▸ <b>Step 2 of 4</b> — Find Aadhaar OTP\n\n"
             "UIDAI sent an OTP to your mobile.\nSend it to locate your Aadhaar number."),
"found_aadhaar": ("✔ <b>Aadhaar located.</b>\n\n"
                  "Number ending <b>{last4}</b>.\n"
                  "◐ Fetching your PDF now…"),
"working_pdf": "◐ Submitting Aadhaar number and solving captcha…",
"ask_otp2": ("▸ <b>Step 3 of 4</b> — Download Verification OTP\n\n"
             "UIDAI sent another OTP.\nSend it to proceed to the download step."),
"ask_otp3": ("▸ <b>Step 4 of 4</b> — Final Download OTP\n\n"
             "One more OTP was sent. Send it to receive your PDF."),
"fail": ("✖ <b>Request failed.</b>\n\nRoute blocked or UIDAI rejected the session.\n"
         "Tap <b>Retry</b> below — the bot rotates to a fresh connection."),
"sending_pdf": "◐ Generating PDF… sending in a moment.",
"done": "✔ <b>Done.</b> Your Aadhaar PDF is above.\nUse /start for another request.",
"cancelled": "✖ Request cancelled. Use /start to begin again.",
}

# ─────────────────────────── PROXY ────────────────────────────
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

def _normalize_proxy(raw: str) -> Optional[dict]:
    if not raw: return None
    raw = raw.strip()
    parts = raw.split(":")
    if len(parts) == 4 and "." in parts[0] and parts[1].isdigit():
        host, port, user, pwd = parts
        return {"server": f"http://{host}:{port}",
                "username": user, "password": pwd}
    if "://" not in raw:
        raw = "http://" + raw
    scheme, rest = raw.split("://", 1)
    if "@" in rest:
        creds, hostport = rest.rsplit("@", 1)
        user, _, pwd = creds.partition(":")
        d = {"server": f"{scheme}://{hostport}"}
        if user:
            d["username"] = user; d["password"] = pwd
        return d
    return {"server": f"{scheme}://{rest}"}

def _proxy_to_url(p: dict) -> str:
    url = p["server"]
    if p.get("username"):
        scheme, hostport = url.split("://", 1)
        url = f"{scheme}://{p['username']}:{p['password']}@{hostport}"
    return url

def pick_proxy() -> tuple[Optional[dict], Optional[str]]:
    live = [p for p in PROXIES if p not in _bad]
    if not live:
        _bad.clear(); live = PROXIES
    if not live:
        return None, None
    raw = _rng.choice(live)
    return _normalize_proxy(raw), raw

def mark_bad(raw: Optional[str]) -> None:
    if raw:
        _bad.add(raw); log.warning("proxy dead: %s", raw)

async def proxy_health(conc: int = 40, timeout: float = 8.0) -> int:
    if not PROXIES:
        log.warning("no proxies configured — UIDAI will reset"); return 0
    async def one(s, raw):
        p = _normalize_proxy(raw)
        if not p: return raw, False
        try:
            async with s.get("https://api.ipify.org?format=json",
                             proxy=_proxy_to_url(p),
                             timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return raw, r.status == 200
        except Exception:
            return raw, False
    conn = aiohttp.TCPConnector(limit=conc, ssl=False)
    async with aiohttp.ClientSession(connector=conn) as s:
        res = await asyncio.gather(*[one(s, p) for p in PROXIES])
    _bad.update(p for p, ok in res if not ok)
    live = len(PROXIES) - len(_bad)
    log.info("proxy health: %d live / %d total", live, len(PROXIES))
    return live

# ─────────────────────────── CAPSOLVER ────────────────────────
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

# ─────────────────────────── ENGINE ───────────────────────────
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
        live = await proxy_health()
        if live == 0:
            log.error("⚠ no live proxies — UIDAI requests will fail")
    async def stop(self):
        if self.browser: await self.browser.close()
        if self._pw:      await self._pw.stop()
    async def new_session(self):
        last = None
        for _ in range(8):
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

# ─────────────────────────── UIDAI ────────────────────────────
FIND_UID_URL = "https://myaadhaar.uidai.gov.in/find-uid"
PDF_URL      = "https://myaadhaar.uidai.gov.in/genuineaadhaar/copyofAaadhaarNumber"
NAV_TIMEOUT  = 25000

# ⚠ IMPORTANT: selectors below are best-effort generic.
# If UIDAI page structure differs, only these need updating.
SEL_FIND = {
    "mobile_input":  "input[formcontrolname='mobileNumber'], input#mobileNumber, input[placeholder*='Mobile' i], input[name='mobileNumber']",
    "captcha_img":   "img.captcha-img, img[src*='captcha'], img#captchaImage",
    "captcha_input": "input[formcontrolname='captcha'], input#captcha, input[placeholder*='captcha' i], input[name='captcha']",
    "submit":        "button[type='submit'], button:has-text('Send OTP'), button:has-text('Get OTP'), button:has-text('Next')",
    "otp_input":     "input[formcontrolname='otp'], input#otp, input[placeholder*='OTP' i], input[name='otp']",
    "otp_submit":    "button:has-text('Verify'), button:has-text('Submit'), button[type='submit']",
    "aadhaar_result":"input[formcontrolname='aadhaarNumber'], input#aadhaarNumber, span:has-text('XXXX'), .aadhaar-number, [class*='aadhaar']",
}

SEL_PDF = {
    "aadhaar_input": "input[formcontrolname='aadhaarNumber'], input#aadhaarNumber, input[placeholder*='Aadhaar' i]",
    "captcha_img":   "img.captcha-img, img[src*='captcha'], img#captchaImage",
    "captcha_input": "input[formcontrolname='captcha'], input#captcha, input[placeholder*='captcha' i]",
    "submit":        "button[type='submit'], button:has-text('Send OTP'), button:has-text('Next')",
    "otp_input":     "input[formcontrolname='otp'], input#otp, input[placeholder*='OTP' i]",
    "otp_submit":    "button:has-text('Verify'), button:has-text('Submit'), button[type='submit']",
    "download":      "button:has-text('Download'), a:has-text('Download')",
}

async def _solve_img(page: Page, http: aiohttp.ClientSession, sel_img: str) -> str:
    img = await page.query_selector(sel_img)
    if not img: raise RuntimeError("captcha image not found")
    b64 = base64.b64encode(await img.screenshot()).decode()
    return await solve_captcha(http, b64)

# ---- FIND UID STEP ----
async def find_uid_open(page: Page):
    await page.goto(FIND_UID_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    await page.wait_for_selector(SEL_FIND["captcha_img"], timeout=15000)

async def find_uid_submit_mobile(page: Page, http: aiohttp.ClientSession, mobile: str):
    await page.fill(SEL_FIND["mobile_input"], mobile)
    await page.fill(SEL_FIND["captcha_input"],
                    await _solve_img(page, http, SEL_FIND["captcha_img"]))
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=NAV_TIMEOUT):
        await page.click(SEL_FIND["submit"])
    await page.wait_for_selector(SEL_FIND["otp_input"], timeout=20000)

async def find_uid_submit_otp(page: Page, otp: str) -> str:
    """Submits OTP-1, returns the aadhaar number (or masked string)."""
    await page.fill(SEL_FIND["otp_input"], otp)
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=NAV_TIMEOUT):
        await page.click(SEL_FIND["otp_submit"])
    # extract aadhaar number from page
    for sel in SEL_FIND["aadhaar_result"].split(","):
        try:
            el = await page.query_selector(sel.strip())
            if el:
                val = (await el.get_attribute("value")) or (await el.inner_text())
                digits = re.sub(r"\D", "", val or "")
                if len(digits) == 12:
                    return digits
        except Exception:
            continue
    # fallback: scan page text for 12-digit
    body = await page.inner_text("body")
    m = re.search(r"\b(\d{4}[\s-]?\d{4}[\s-]?\d{4})\b", body)
    if m:
        return re.sub(r"\D", "", m.group(1))
    raise RuntimeError("aadhaar number not found on find-uid result page")

# ---- PDF STEP ----
async def pdf_open(page: Page):
    await page.goto(PDF_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    await page.wait_for_selector(SEL_PDF["captcha_img"], timeout=15000)

async def pdf_submit_aadhaar(page: Page, http: aiohttp.ClientSession, aadhaar: str):
    await page.fill(SEL_PDF["aadhaar_input"], aadhaar)
    await page.fill(SEL_PDF["captcha_input"],
                    await _solve_img(page, http, SEL_PDF["captcha_img"]))
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=NAV_TIMEOUT):
        await page.click(SEL_PDF["submit"])
    await page.wait_for_selector(SEL_PDF["otp_input"], timeout=20000)

async def pdf_submit_otp(page: Page, otp: str):
    await page.fill(SEL_PDF["otp_input"], otp)
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=NAV_TIMEOUT):
        await page.click(SEL_PDF["otp_submit"])

async def pdf_download(page: Page, timeout: int = 30000) -> bytes:
    async with page.expect_download(timeout=timeout) as dl:
        await page.click(SEL_PDF["download"])
    d = await dl.value
    path = await d.path()
    with open(path, "rb") as f: return f.read()

# ─────────────────────────── SESSION ──────────────────────────
@dataclass
class Sess:
    user_id: int
    stage: Literal["awaiting_mobile", "awaiting_otp1",
                   "awaiting_otp2", "awaiting_otp3"]
    mobile: str = ""
    aadhaar: str = ""
    ctx: object = None
    page: object = None
    created: float = field(default_factory=time.time)

SESSIONS: dict[int, Sess] = {}

# ─────────────────────────── UI ───────────────────────────────
def gate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Join Channel", url=CHANNEL_URL)],
        [InlineKeyboardButton("🔁 Retry", callback_data="gate:retry")],
    ])

def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Mobile → Aadhaar PDF", callback_data="start:mobile")],
    ])

def retry_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Retry", callback_data="gate:retry")]])

async def is_member(ctx: ContextTypes.DEFAULT_TYPE, uid: int) -> bool:
    try:
        m = await ctx.bot.get_chat_member(CHANNEL_ID, uid)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False

def eng(ctx): return ctx.application.bot_data.get("engine")
def http(ctx): return ctx.application.bot_data["http"]
def digits(s): return re.sub(r"\D", "", s or "")

# ─────────────────────────── HANDLERS ─────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not eng(ctx):
        await update.effective_message.reply_text(
            "⚠ Engine is starting. Try again in 30 seconds.")
        return
    if await is_member(ctx, update.effective_user.id):
        await update.effective_message.reply_text(T["menu"], parse_mode="HTML")
        await update.effective_message.reply_text("Tap below 👇", reply_markup=menu_kb())
    else:
        await update.effective_message.reply_text(T["gate"], parse_mode="HTML",
                                                  reply_markup=gate_kb())

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
            await q.edit_message_text(T["gate_still"], parse_mode="HTML",
                                      reply_markup=gate_kb())
            return
        await q.edit_message_text(T["menu"], parse_mode="HTML")
        await q.message.reply_text("Tap below 👇", reply_markup=menu_kb())
    elif data == "start:mobile":
        SESSIONS[q.from_user.id] = Sess(user_id=q.from_user.id, stage="awaiting_mobile")
        await q.edit_message_text(T["ask_mobile"], parse_mode="HTML")

async def _fail(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int, e: Exception):
    s = SESSIONS.pop(uid, None)
    if s and s.ctx:
        try: await s.ctx.close()
        except Exception: pass
    try:
        await update.effective_message.reply_text(T["fail"], parse_mode="HTML",
                                                  reply_markup=retry_kb())
    except Exception: pass
    try: await ctx.bot.send_message(ADMIN_ID, f"[fail] uid={uid} err={e}")
    except Exception: pass

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not eng(ctx):
        await update.effective_message.reply_text(
            "⚠ Engine is starting. Try again in 30 seconds.")
        return
    uid = update.effective_user.id
    msg = update.effective_message
    s = SESSIONS.get(uid)
    if not s: return

    # ── step 1: mobile ──
    if s.stage == "awaiting_mobile":
        m = digits(msg.text)
        if len(m) != 10:
            await msg.reply_text(T["bad_mobile"]); return
        s.mobile = m
        await msg.reply_text(T["working_find"], parse_mode="HTML")
        await msg.chat.send_action(ChatAction.TYPING)
        c_, page, raw, err = await eng(ctx).new_session()
        if err:
            await _fail(update, ctx, uid, err); return
        s.ctx, s.page = c_, page
        try:
            await find_uid_open(page)
            await find_uid_submit_mobile(page, http(ctx), s.mobile)
        except Exception as e:
            await _fail(update, ctx, uid, e); return
        s.stage = "awaiting_otp1"
        await msg.reply_text(T["ask_otp1"], parse_mode="HTML")
        return

    # ── step 2: OTP-1 (find-uid) ──
    if s.stage == "awaiting_otp1":
        otp = digits(msg.text)
        if len(otp) < 4:
            await msg.reply_text("✖ OTP looks wrong. Send it again."); return
        try:
            s.aadhaar = await find_uid_submit_otp(s.page, otp)
        except Exception as e:
            await _fail(update, ctx, uid, e); return
        # close find-uid session, open PDF session on fresh proxy
        try: await s.ctx.close()
        except Exception: pass
        s.ctx = s.page = None
        await msg.reply_text(
            T["found_aadhaar"].format(last4=s.aadhaar[-4:]), parse_mode="HTML")
        await msg.chat.send_action(ChatAction.TYPING)

        c_, page, raw, err = await eng(ctx).new_session()
        if err:
            await _fail(update, ctx, uid, err); return
        s.ctx, s.page = c_, page
        try:
            await pdf_open(page)
            await pdf_submit_aadhaar(page, http(ctx), s.aadhaar)
        except Exception as e:
            await _fail(update, ctx, uid, e); return
        s.stage = "awaiting_otp2"
        await msg.reply_text(T["ask_otp2"], parse_mode="HTML")
        return

    # ── step 3: OTP-2 (PDF verification) ──
    if s.stage == "awaiting_otp2":
        otp = digits(msg.text)
        if len(otp) < 4:
            await msg.reply_text("✖ OTP looks wrong. Send it again."); return
        try:
            await pdf_submit_otp(s.page, otp)
        except Exception as e:
            await _fail(update, ctx, uid, e); return
        s.stage = "awaiting_otp3"
        await msg.reply_text(T["ask_otp3"], parse_mode="HTML")
        return

    # ── step 4: OTP-3 (download) ──
    if s.stage == "awaiting_otp3":
        otp = digits(msg.text)
        if len(otp) < 4:
            await msg.reply_text("✖ OTP looks wrong. Send it again."); return
        await msg.reply_text(T["sending_pdf"], parse_mode="HTML")
        await msg.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        try:
            await pdf_submit_otp(s.page, otp)
            pdf = await pdf_download(s.page)
        except Exception as e:
            await _fail(update, ctx, uid, e); return
        SESSIONS.pop(uid, None)
        if s.ctx:
            try: await s.ctx.close()
            except Exception: pass
        fname = f"AADHAAR_{s.aadhaar[-4:]}.pdf"
        await msg.reply_document(pdf, filename=fname,
                                 caption=T["done"], parse_mode="HTML")
        return

# ─────────────────────────── LIFECYCLE ────────────────────────
async def post_init(app: Application):
    app.bot_data["engine"] = None
    app.bot_data["http"] = aiohttp.ClientSession()
    try:
        e = Engine()
        await e.start()
        app.bot_data["engine"] = e
        log.info("engine ready")
    except Exception as ex:
        log.exception("engine failed: %s", ex)
        try: await app.bot.send_message(ADMIN_ID, f"⚠ engine start failed:\n{ex}")
        except Exception: pass

async def post_shutdown(app: Application):
    e = app.bot_data.get("engine")
    if e:
        try: await e.stop()
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
