# -*- coding: utf-8 -*-
"""
🤖 ربات هوش مصنوعی تلگرام
- جستجوی زنده در وب
- فرستادن عکس واقعی از وب (هر چی بگی میره پیدا می‌کنه و می‌فرسته)
- تحلیل عکس (شیء، حیوان، گیاه، مکان، متن، محصول، ...)
- پنل مدیریت پیشرفته با دکمه‌های رنگی
"""
import asyncio
import base64
import csv
import html
import io
import ipaddress
import logging
import os
import re
import socket
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

import httpx
from ddgs import DDGS
from google import genai
from google.genai import errors as gerrors
from google.genai import types
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
    LinkPreviewOptions, Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ═══════════════════════ تنظیمات ═══════════════════════
BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ENV_ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
MODEL = os.getenv("MODEL", "gemini-2.5-flash")
DB_PATH = os.getenv("DB_PATH", "bot.db")
BTN_ICON = os.getenv("BTN_ICON_ID", "").strip()  # اختیاری: آیدی کاستوم‌ایموجی برای آیکون دکمه‌ها

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
LOG = logging.getLogger("bot")

gclient = genai.Client(api_key=GEMINI_API_KEY)
NOPREV = LinkPreviewOptions(is_disabled=True)
HIST: dict[int, list] = {}
LOCKS: dict[int, asyncio.Lock] = {}

# ═══════════════════════ دیتابیس ═══════════════════════
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT, username TEXT, joined TEXT, last_seen TEXT,
  banned INTEGER DEFAULT 0, msgs INTEGER DEFAULT 0, photos INTEGER DEFAULT 0,
  images INTEGER DEFAULT 0, today_count INTEGER DEFAULT 0, today_date TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS channels(chat TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, kind TEXT, text TEXT, ts TEXT);
""")

DEFAULT_WELCOME = (
    "سلام {name} 👋\n\n"
    "من یه <b>دستیار هوشمندم</b> 🤖\n\n"
    "🔎 هر چیزی بخوای تو وب پیدا می‌کنم\n"
    "🖼 عکسِ واقعیِ هر چیزی رو می‌فرستم\n"
    "🧠 عکس بفرست تا تحلیلش کنم (شیء، حیوان، گیاه، مکان، متن، محصول و...)\n"
    "💬 هر سوالی داری بپرس\n\n"
    "یکی از گزینه‌ها رو بزن یا مستقیم پیامت رو بنویس 👇"
)
DEFAULTS = {
    "bot_on": "1", "ai_chat": "1", "web_search": "1", "img_search": "1",
    "photo_ai": "1", "safe": "1", "daily_limit": "30", "max_images": "4",
    "welcome": DEFAULT_WELCOME,
}
FEATURES = [
    ("ai_chat", "💬 چت هوشمند"), ("web_search", "🔎 جستجوی وب"),
    ("img_search", "🖼 دریافت عکس از وب"), ("photo_ai", "🧠 تحلیل عکس"),
    ("safe", "🛡 سرچ امن"),
]


def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def today(): return datetime.now().strftime("%Y-%m-%d")
def run(sql, args=()):
    cur = db.execute(sql, args); db.commit(); return cur
def fetch(sql, args=()): return db.execute(sql, args).fetchall()
def fetch1(sql, args=()): return db.execute(sql, args).fetchone()
def get(k):
    r = fetch1("SELECT value FROM settings WHERE key=?", (k,))
    return r["value"] if r else DEFAULTS[k]
def put(k, v):
    run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
def is_admin(uid): return uid in ENV_ADMINS or fetch1("SELECT 1 FROM admins WHERE id=?", (uid,)) is not None
def esc(s): return html.escape(str(s or ""))


def register(user):
    run("INSERT OR IGNORE INTO users(id,name,username,joined,last_seen) VALUES(?,?,?,?,?)",
        (user.id, user.full_name, user.username or "", now(), now()))
    run("UPDATE users SET name=?, username=?, last_seen=? WHERE id=?",
        (user.full_name, user.username or "", now(), user.id))


def check_quota(uid):
    lim = int(get("daily_limit"))
    if is_admin(uid) or lim <= 0:
        return True
    r = fetch1("SELECT today_count, today_date FROM users WHERE id=?", (uid,))
    used = r["today_count"] if r and r["today_date"] == today() else 0
    return used < lim


def bump(uid, kind, text, n_imgs=0):
    r = fetch1("SELECT today_count, today_date FROM users WHERE id=?", (uid,))
    cnt = (r["today_count"] if r["today_date"] == today() else 0) + 1
    run("UPDATE users SET msgs=msgs+1, photos=photos+?, images=images+?, today_count=?, today_date=? WHERE id=?",
        (1 if kind == "photo" else 0, n_imgs, cnt, today(), uid))
    run("INSERT INTO logs(uid,kind,text,ts) VALUES(?,?,?,?)", (uid, kind, (text or "")[:200], now()))


def stats():
    r = fetch1("SELECT COUNT(*) c, SUM(banned) b, SUM(msgs) m, SUM(photos) p, SUM(images) i FROM users")
    return dict(
        users=r["c"], banned=r["b"] or 0, msgs=r["m"] or 0, photos=r["p"] or 0, images=r["i"] or 0,
        new=fetch1("SELECT COUNT(*) c FROM users WHERE joined LIKE ?", (today() + "%",))["c"],
        active=fetch1("SELECT COUNT(*) c FROM users WHERE last_seen LIKE ?", (today() + "%",))["c"],
        channels=fetch1("SELECT COUNT(*) c FROM channels")["c"],
    )


# ═══════════════════════ دکمه‌های رنگی ═══════════════════════
def btn(text, data=None, style=None, url=None):
    """style: success (سبز) | danger (قرمز) | primary (آبی)"""
    kw = {}
    if style:
        kw["style"] = style
    if BTN_ICON:
        kw["icon_custom_emoji_id"] = BTN_ICON
    if url:
        return InlineKeyboardButton(text, url=url, api_kwargs=kw or None)
    return InlineKeyboardButton(text, callback_data=data, api_kwargs=kw or None)


def back_kb(label="🔙 پنل مدیریت", data="a:main"):
    return InlineKeyboardMarkup([[btn(label, data, "primary")]])


def main_menu(uid):
    rows = [
        [btn("🔎 جستجو در وب", "u:web", "primary"), btn("🖼 دریافت عکس", "u:img", "success")],
        [btn("🧠 تحلیل عکس", "u:photo", "primary"), btn("💬 چت هوشمند", "u:chat", "success")],
        [btn("👤 پروفایل من", "u:me", "primary"), btn("📜 تاریخچه", "u:hist")],
        [btn("🧹 پاک‌کردن حافظه", "u:clear", "danger"), btn("❓ راهنما", "u:help", "primary")],
    ]
    if is_admin(uid):
        rows.append([btn("👑 پنل مدیریت", "a:main", "danger")])
    return InlineKeyboardMarkup(rows)


def user_back():
    return InlineKeyboardMarkup([[btn("🏠 منوی اصلی", "u:main", "primary")]])


# ═══════════════════════ ابزارهای ارسال ═══════════════════════
TAG_RE = re.compile(r"</?(b|i|u|s|code|pre|a|tg-spoiler|blockquote)[^>]*>")
def plain(t): return html.unescape(TAG_RE.sub("", t))


async def send_long(msg, text, markup=None):
    text = (text or "").strip() or "…"
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)]
    for i, c in enumerate(chunks):
        mk = markup if i == len(chunks) - 1 else None
        try:
            await msg.reply_text(c, parse_mode=ParseMode.HTML, reply_markup=mk, link_preview_options=NOPREV)
        except BadRequest:
            await msg.reply_text(plain(c), reply_markup=mk, link_preview_options=NOPREV)


async def edit(qy, text, markup=None):
    try:
        await qy.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup, link_preview_options=NOPREV)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        await qy.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup, link_preview_options=NOPREV)


async def keep_typing(chat, stop: asyncio.Event):
    while not stop.is_set():
        try:
            await chat.send_action(ChatAction.TYPING)
        except TelegramError:
            pass
        try:
            await asyncio.wait_for(stop.wait(), 4)
        except asyncio.TimeoutError:
            pass


# ═══════════════════════ جوین اجباری / گیت ═══════════════════════
async def channel_link(bot, ch):
    if ch.startswith("@"):
        return f"https://t.me/{ch[1:]}"
    try:
        c = await bot.get_chat(ch)
        if c.username:
            return f"https://t.me/{c.username}"
        return c.invite_link or await bot.export_chat_invite_link(ch)
    except TelegramError:
        return None


async def missing_channels(bot, uid):
    missing = []
    for r in fetch("SELECT chat FROM channels"):
        try:
            m = await bot.get_chat_member(r["chat"], uid)
            if m.status in ("left", "kicked"):
                missing.append(r["chat"])
        except TelegramError:
            continue  # ربات ادمین کانال نیست؛ نادیده بگیر
    return missing


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE, check_join=True) -> bool:
    user = update.effective_user
    msg = update.effective_message
    if not user or user.is_bot:
        return False
    register(user)
    if is_admin(user.id):
        return True
    r = fetch1("SELECT banned FROM users WHERE id=?", (user.id,))
    if r and r["banned"]:
        await msg.reply_text("🚫 دسترسی شما به این ربات مسدود شده است.")
        return False
    if get("bot_on") != "1":
        await msg.reply_text("🔧 ربات موقتاً در حال بروزرسانیه. کمی بعد برگرد 🙏")
        return False
    if check_join:
        missing = await missing_channels(context.bot, user.id)
        if missing:
            rows = []
            for ch in missing:
                link = await channel_link(context.bot, ch)
                if link:
                    rows.append([btn(f"📢 عضویت در {ch}", style="primary", url=link)])
            rows.append([btn("✅ عضو شدم، بررسی کن", "u:chk", "success")])
            await msg.reply_text("📌 برای استفاده از ربات، اول تو کانال(های) زیر عضو شو:",
                                 reply_markup=InlineKeyboardMarkup(rows))
            return False
    return True


# ═══════════════════════ هسته هوش مصنوعی ═══════════════════════
SYSTEM = """You are a smart, friendly assistant living inside a Telegram bot. Today's date: {date}.
- Reply in the user's language (Persian/Farsi by default). Be concise and clear; this is a phone chat.
- Format only with Telegram HTML: <b>, <i>, <code>, <a href="...">. Never use Markdown (no ** or #). Escape stray < > & as &lt; &gt; &amp;.
- If the user wants you to find, look up or check something (news, prices, facts, products, places, how-tos, anything recent), call web_search first (use news=true for news) and answer from the results; use read_page to open a promising result when snippets are not enough. Mention sources briefly with links.
- If the user asks for a picture/photo/image of something (e.g. "send me a photo of ..."), call send_images with a precise query (English queries usually work best), then add a short one-line comment. Never paste image URLs yourself.
- For a photo the user sends: identify and explain what you see - objects, animals, plants, landmarks, artworks, products, food, text (read/translate if asked), diagrams, etc. If it shows a real person, never identify them from their face; describe clothing, setting and expression, and rely only on explicit context (caption, visible text/name tags). If the user says who it is, treat it as their claim and, if asked, search the web about that person.
- If you are unsure, say so honestly instead of guessing."""

WEB_DECL = types.FunctionDeclaration(
    name="web_search",
    description="Search the web (text results or news) and return titles, links and snippets.",
    parameters={"type": "OBJECT", "properties": {
        "query": {"type": "STRING", "description": "Search query"},
        "news": {"type": "BOOLEAN", "description": "true to search recent news"}},
        "required": ["query"]})
READ_DECL = types.FunctionDeclaration(
    name="read_page",
    description="Open a web page from search results and return its main text.",
    parameters={"type": "OBJECT", "properties": {"url": {"type": "STRING"}}, "required": ["url"]})
IMG_DECL = types.FunctionDeclaration(
    name="send_images",
    description="Search the web for real photos and send them to the user in Telegram. Use whenever the user wants to see or receive a picture/photo of something.",
    parameters={"type": "OBJECT", "properties": {
        "query": {"type": "STRING", "description": "Precise image search query"},
        "count": {"type": "INTEGER", "description": "How many photos (1-8)"}},
        "required": ["query"]})

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def _public(url: str) -> bool:
    """جلوگیری از دسترسی به آدرس‌های داخلی (SSRF)"""
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        for info in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


async def web_search(query: str, news: bool = False):
    def go():
        with DDGS() as d:
            return list(d.news(query, max_results=6) if news else d.text(query, max_results=6))
    try:
        rows = await asyncio.to_thread(go)
    except Exception as e:
        LOG.warning("web search failed: %s", e)
        return [{"error": "search failed"}]
    return [{"title": r.get("title", ""), "url": r.get("href") or r.get("url", ""),
             "snippet": (r.get("body") or "")[:400], "date": r.get("date", "")} for r in rows]


async def read_page(url: str):
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers=UA) as http:
            for _ in range(4):
                if not await asyncio.to_thread(_public, url):
                    return {"error": "blocked url"}
                r = await http.get(url)
                if r.is_redirect and r.headers.get("location"):
                    url = str(httpx.URL(url).join(r.headers["location"]))
                    continue
                break
            t = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", r.text)
            t = re.sub(r"(?s)<[^>]+>", " ", t)
            t = html.unescape(re.sub(r"\s+", " ", t)).strip()
            return {"url": url, "text": t[:6000]}
    except Exception:
        return {"error": "could not open page"}


def md_to_html(t: str) -> str:
    """اگه مدل مارک‌داون نوشت، به HTML تلگرام تبدیلش کن"""
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"(?m)^#{1,6}\s*(.+)$", r"<b>\1</b>", t)
    t = re.sub(r"(?m)^\s*[\*\-]\s+", "• ", t)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    return t


async def fetch_images(query: str, n: int):
    safe = "on" if get("safe") == "1" else "moderate"

    def search():
        with DDGS() as d:
            return list(d.images(query, safesearch=safe, max_results=max(n * 4, 8)))

    try:
        results = await asyncio.to_thread(search)
    except Exception as e:
        LOG.warning("image search failed: %s", e)
        return []
    out = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=UA) as http:
        for r in results:
            if len(out) >= n:
                break
            url = r.get("image")
            if not url or not await asyncio.to_thread(_public, url):
                continue
            try:
                resp = await http.get(url)
                ct = resp.headers.get("content-type", "")
                if (resp.status_code == 200 and ct.startswith("image/") and "svg" not in ct
                        and 3_000 < len(resp.content) < 9_000_000):
                    out.append((resp.content, r.get("title", ""), r.get("url", "")))
            except Exception:
                continue
    return out


async def send_images(msg, imgs):
    if not imgs:
        return 0

    def cap(t, s):
        c = f"🖼 {esc(t[:120])}" if t else "🖼"
        if s:
            c += f'\n<a href="{html.escape(s, quote=True)}">🔗 منبع</a>'
        return c

    if len(imgs) >= 2:
        try:
            media = [InputMediaPhoto(io.BytesIO(b), caption=cap(t, s) if i == 0 else None,
                                     parse_mode=ParseMode.HTML)
                     for i, (b, t, s) in enumerate(imgs[:10])]
            await msg.reply_media_group(media)
            return len(media)
        except TelegramError as e:
            LOG.warning("media group failed: %s", e)
    sent = 0
    for b, t, s in imgs:
        try:
            await msg.reply_photo(io.BytesIO(b), caption=cap(t, s), parse_mode=ParseMode.HTML)
            sent += 1
        except TelegramError:
            continue
    return sent


async def ask_ai(history, allow_web, allow_img, max_imgs):
    decls = []
    if allow_web:
        decls += [WEB_DECL, READ_DECL]
    if allow_img:
        decls.append(IMG_DECL)
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM.format(date=today()), max_output_tokens=2048,
        tools=[types.Tool(function_declarations=decls)] if decls else None,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    contents = list(history)
    collected, final = [], ""
    for _ in range(6):
        resp = await gclient.aio.models.generate_content(model=MODEL, contents=contents, config=cfg)
        cand = resp.candidates[0] if resp.candidates else None
        parts = cand.content.parts if cand and cand.content and cand.content.parts else []
        text = "".join(p.text for p in parts if p.text and not p.thought).strip()
        calls = [p.function_call for p in parts if p.function_call]
        if not calls:
            final = text or final
            break
        contents.append(cand.content)
        out = []
        for fc in calls:
            a = dict(fc.args or {})
            if fc.name == "web_search":
                res = await web_search(str(a.get("query", "")), bool(a.get("news", False)))
            elif fc.name == "read_page":
                res = await read_page(str(a.get("url", "")))
            elif fc.name == "send_images":
                n = max(1, min(int(a.get("count", 3) or 3), max_imgs))
                imgs = await fetch_images(str(a.get("query", "")), n)
                collected.extend(imgs)
                res = f"{len(imgs)} images found and will be attached." if imgs else "No images could be fetched."
            else:
                res = "unknown tool"
            out.append(types.Part.from_function_response(name=fc.name, response={"result": res}))
        contents.append(types.Content(role="user", parts=out))
    if not final and not collected:
        final = "😕 نتونستم جواب مناسبی بسازم. سوالت رو یه‌جور دیگه بپرس."
    return md_to_html(final), collected


def followup_kb():
    row = []
    if get("img_search") == "1":
        row.append(btn("🖼 عکس مرتبط", "u:moreimg", "success"))
    row.append(btn("🔬 جزئیات بیشتر", "u:deeper", "primary"))
    return InlineKeyboardMarkup([row, [btn("🏠 منو", "u:main", "primary")]])


async def handle_ai(update: Update, context: ContextTypes.DEFAULT_TYPE, content, kind, log_text):
    msg, user = update.effective_message, update.effective_user
    uid = user.id
    admin = is_admin(uid)
    if kind == "photo" and get("photo_ai") != "1" and not admin:
        return await msg.reply_text("🚫 تحلیل عکس موقتاً غیرفعاله.")
    if kind != "photo" and get("ai_chat") != "1" and not admin:
        return await msg.reply_text("🚫 چت هوشمند موقتاً غیرفعاله.")
    if not check_quota(uid):
        return await msg.reply_text(
            f"⏳ سهمیه امروزت ({get('daily_limit')} درخواست) تموم شد. فردا دوباره بیا 🌙",
            reply_markup=user_back())

    lock = LOCKS.setdefault(uid, asyncio.Lock())
    if lock.locked():
        return await msg.reply_text("⏳ هنوز دارم جواب پیام قبلیت رو آماده می‌کنم...")

    async with lock:
        status = await msg.reply_text("🧠 دارم فکر می‌کنم و جستجو می‌کنم...")
        stop = asyncio.Event()
        typing = asyncio.create_task(keep_typing(msg.chat, stop))

        h = HIST.setdefault(uid, [])
        for i, m in enumerate(h):  # عکس‌های قبلی رو از حافظه حذف کن
            if any(p.inline_data for p in m.parts):
                h[i] = types.Content(role=m.role, parts=[
                    types.Part.from_text(text="[تصویر قبلی]") if p.inline_data else p for p in m.parts])
        user_parts = [types.Part.from_text(text=content)] if isinstance(content, str) else content
        h.append(types.Content(role="user", parts=user_parts))
        del h[:-12]
        while h and h[0].role != "user":
            h.pop(0)

        try:
            text, imgs = await ask_ai(
                h, allow_web=get("web_search") == "1", allow_img=get("img_search") == "1",
                max_imgs=int(get("max_images")))
        except gerrors.APIError as e:
            LOG.warning("Gemini API error %s: %s", getattr(e, "code", "?"), e)
            h.pop()
            stop.set(); await typing
            if getattr(e, "code", None) == 429:
                return await status.edit_text("🚦 سقف درخواست‌های رایگان پر شده؛ یه دقیقه دیگه دوباره امتحان کن.")
            return await status.edit_text("❌ مشکلی تو ارتباط با هوش مصنوعی پیش اومد. دوباره امتحان کن.")
        except Exception as e:
            LOG.exception("AI error: %s", e)
            h.pop()
            stop.set(); await typing
            return await status.edit_text("❌ مشکلی پیش اومد. دوباره امتحان کن.")
        finally:
            stop.set()

        h.append(types.Content(role="model", parts=[types.Part.from_text(text=text or "[تصاویر ارسال شد]")]))
        try:
            await status.delete()
        except TelegramError:
            pass
        if text:
            await send_long(msg, text, followup_kb() if not imgs else None)
        sent = await send_images(msg, imgs) if imgs else 0
        if imgs:
            await msg.reply_text("چیز دیگه‌ای هم می‌خوای؟ 👇", reply_markup=followup_kb())
        if kind == "chat" and sent:
            kind = "img"
        bump(uid, kind, log_text, sent)
        context.user_data["last_q"] = log_text


PHOTO_Q = ("این تصویر چیه؟ توضیح بده و هر چیزی که قابل شناسایی هست (شیء، حیوان، گیاه، مکان، محصول، "
           "اثر هنری، غذا، متن) رو بگو. اگه آدمه، چهره‌اش رو شناسایی نکن ولی بگو چی می‌بینی.")
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MODE_PREFIX = {
    "web": "(کاربر جستجوی وب می‌خواد؛ حتماً از web_search استفاده کن) ",
    "img": "(کاربر عکس واقعی می‌خواد؛ حتماً send_images رو صدا بزن) ",
}


async def process_image(update, context, data: bytes, mime: str, caption: str):
    if len(data) > 5_000_000:
        return await update.effective_message.reply_text("📦 عکس بزرگه (بیشتر از ۵ مگابایت). یه نسخه کوچیک‌تر بفرست.")
    content = [
        types.Part.from_bytes(data=data, mime_type=mime),
        types.Part.from_text(text=caption.strip() or PHOTO_Q),
    ]
    await handle_ai(update, context, content, "photo", caption.strip() or "تحلیل عکس")


# ═══════════════════════ هندلرهای کاربر ═══════════════════════
HELP = (
    "❓ <b>راهنما</b>\n━━━━━━━━━━━━━━\n"
    "🔎 <b>جستجو:</b> مثلاً «قیمت امروز بیت‌کوین» یا «بهترین لپ‌تاپ زیر ۳۰ میلیون»\n"
    "🖼 <b>عکس بخواه:</b> «عکس گربه پرشین بفرست» / «چند تا عکس از برج میلاد»\n"
    "🧠 <b>تحلیل عکس:</b> عکس بفرست (می‌تونی زیرش سوالتو بنویسی): «این چه گیاهیه؟»، «این متن چی میگه؟»، «این کجاست؟»\n"
    "💬 <b>چت:</b> ربات حرفات رو یادش می‌مونه؛ برای شروع تازه «پاک‌کردن حافظه» رو بزن.\n\n"
    "ℹ️ از روی چهره آدم‌ها کسی رو شناسایی نمی‌کنم، ولی هر چیز دیگه‌ای رو می‌تونم تحلیل کنم. "
    "اگه اسم شخص رو بگی، درباره‌اش تو وب جستجو می‌کنم."
)


def welcome_text(user):
    return get("welcome").replace("{name}", esc(user.first_name))


async def cmd_start(update, context):
    if not await gate(update, context):
        return
    context.user_data.pop("state", None)
    await update.effective_message.reply_text(
        welcome_text(update.effective_user), parse_mode=ParseMode.HTML,
        reply_markup=main_menu(update.effective_user.id))


async def cmd_help(update, context):
    if await gate(update, context):
        await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML, reply_markup=user_back())


async def cmd_panel(update, context):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.effective_message.reply_text("⛔ فقط ادمین‌ها به پنل دسترسی دارن.")
    register(update.effective_user)
    await update.effective_message.reply_text(
        admin_home_text(), parse_mode=ParseMode.HTML, reply_markup=admin_home_markup())


async def on_text(update, context):
    msg, uid = update.effective_message, update.effective_user.id
    state = context.user_data.get("state")
    if state and is_admin(uid):
        return await admin_input(update, context, state)
    if not await gate(update, context):
        return
    text = (msg.text or "").strip()
    if not text:
        return
    if len(text) > 3500:
        return await msg.reply_text("✂️ پیامت خیلی طولانیه؛ کوتاه‌ترش کن.")
    mode = context.user_data.pop("mode", None)
    if mode == "photo":
        context.user_data["mode"] = "photo"
        return await msg.reply_text("📷 الان باید <b>عکس</b> بفرستی (می‌تونی زیرش سوالتو بنویسی).",
                                    parse_mode=ParseMode.HTML)
    kind = mode if mode in ("web", "img") else "chat"
    await handle_ai(update, context, MODE_PREFIX.get(mode, "") + text, kind, text)


async def on_photo(update, context):
    msg, uid = update.effective_message, update.effective_user.id
    if is_admin(uid) and context.user_data.get("state") == "bc":
        return await capture_bc(update, context)
    if not await gate(update, context):
        return
    context.user_data.pop("mode", None)
    f = await msg.photo[-1].get_file()
    data = bytes(await f.download_as_bytearray())
    await process_image(update, context, data, "image/jpeg", msg.caption or "")


async def on_other(update, context):
    msg, uid = update.effective_message, update.effective_user.id
    if is_admin(uid) and context.user_data.get("state") == "bc":
        return await capture_bc(update, context)
    if not await gate(update, context):
        return
    doc = msg.document
    if doc and doc.mime_type in ALLOWED_MIME:
        f = await doc.get_file()
        data = bytes(await f.download_as_bytearray())
        return await process_image(update, context, data, doc.mime_type, msg.caption or "")
    await msg.reply_text("فعلاً فقط <b>متن</b> و <b>عکس</b> رو می‌فهمم 🙏", parse_mode=ParseMode.HTML)


async def on_cb(update, context):
    qy = update.callback_query
    data = qy.data or ""
    uid = qy.from_user.id
    if data.startswith("a:"):
        if not is_admin(uid):
            return await qy.answer("⛔ دسترسی نداری", show_alert=True)
        await qy.answer()
        register(qy.from_user)
        return await admin_cb(update, context, data[2:])
    if data != "u:chk":
        await qy.answer()
    if not await gate(update, context, check_join=(data != "u:chk")):
        return
    await user_cb(update, context, data[2:])


async def user_cb(update, context, data):
    qy, user = update.callback_query, update.callback_query.from_user
    uid, ud = user.id, context.user_data
    if data == "chk":
        if await missing_channels(context.bot, uid):
            return await qy.answer("❌ هنوز تو همه کانال‌ها عضو نشدی!", show_alert=True)
        await qy.answer("✅ عالی!")
        return await edit(qy, welcome_text(user), main_menu(uid))
    if data == "main":
        ud.pop("mode", None)
        return await edit(qy, welcome_text(user), main_menu(uid))
    prompts = {
        "web": ("🔎 <b>جستجو در وب</b>\n\nهر چیزی که می‌خوای پیدا کنم رو بنویس.\nمثلاً: <i>آخرین خبر از ...</i>"),
        "img": ("🖼 <b>دریافت عکس</b>\n\nبنویس عکسِ چی می‌خوای؛ می‌رم از وب پیدا می‌کنم و می‌فرستم.\nمثلاً: <i>عکس پلنگ ایرانی</i>"),
        "photo": ("🧠 <b>تحلیل عکس</b>\n\nعکستو بفرست! زیرش می‌تونی سوالتو هم بنویسی.\nمثلاً: <i>این چه گیاهیه؟</i> یا <i>این کجاست؟</i>"),
        "chat": ("💬 <b>چت هوشمند</b>\n\nهر چی تو ذهنته بپرس 😊"),
    }
    if data in prompts:
        ud["mode"] = data if data != "chat" else None
        if data == "chat":
            ud.pop("mode", None)
        return await edit(qy, prompts[data], user_back())
    if data == "me":
        r = fetch1("SELECT * FROM users WHERE id=?", (uid,))
        lim = int(get("daily_limit"))
        used = r["today_count"] if r["today_date"] == today() else 0
        quota = "نامحدود ♾" if is_admin(uid) or lim <= 0 else f"{used} از {lim}"
        return await edit(qy, (
            f"👤 <b>پروفایل من</b>\n━━━━━━━━━━━━━━\n"
            f"🆔 آیدی: <code>{uid}</code>\n📅 عضویت: {r['joined'][:10]}\n"
            f"⚡️ مصرف امروز: <b>{quota}</b>\n💬 کل پیام‌ها: {r['msgs']}\n"
            f"🧠 عکس‌های تحلیل‌شده: {r['photos']}\n🖼 عکس‌های دریافتی: {r['images']}"), user_back())
    if data == "hist":
        rows = fetch("SELECT kind,text,ts FROM logs WHERE uid=? ORDER BY id DESC LIMIT 10", (uid,))
        icons = {"chat": "💬", "web": "🔎", "img": "🖼", "photo": "🧠"}
        body = "\n".join(f"{icons.get(r['kind'], '•')} {esc(r['text'][:60])}  <i>({r['ts'][5:16]})</i>" for r in rows)
        return await edit(qy, "📜 <b>۱۰ درخواست آخر</b>\n━━━━━━━━━━━━━━\n" + (body or "هنوز چیزی نپرسیدی."), user_back())
    if data == "clear":
        HIST.pop(uid, None)
        return await edit(qy, "🧹 حافظه گفتگو پاک شد. از اول شروع کنیم!", user_back())
    if data == "help":
        return await edit(qy, HELP, user_back())
    if data == "moreimg":
        q = ud.get("last_q")
        if not q:
            return await qy.message.reply_text("اول یه چیزی بپرس 🙂")
        wait = await qy.message.reply_text("🖼 دارم عکس پیدا می‌کنم...")
        imgs = await fetch_images(q, int(get("max_images")))
        await wait.delete()
        if not await send_images(qy.message, imgs):
            await qy.message.reply_text("😕 عکس مناسبی پیدا نکردم.")
        else:
            bump(uid, "img", q, len(imgs))
        return
    if data == "deeper":
        return await handle_ai(update, context,
                               "درباره همین موضوع جزئیات بیشتر، نکات تکمیلی و منابع بده.", "web", "جزئیات بیشتر")


# ═══════════════════════ پنل مدیریت ═══════════════════════
def admin_home_text():
    s = stats()
    on = get("bot_on") == "1"
    return (f"👑 <b>پنل مدیریت</b>\n━━━━━━━━━━━━━━\n"
            f"👥 کاربران: <b>{s['users']}</b>   🟢 فعال امروز: <b>{s['active']}</b>\n"
            f"🆕 جدید امروز: <b>{s['new']}</b>   🚫 بن‌شده: <b>{s['banned']}</b>\n"
            f"🤖 وضعیت ربات: <b>{'روشن ✅' if on else 'خاموش ⛔'}</b>")


def admin_home_markup():
    on = get("bot_on") == "1"
    return InlineKeyboardMarkup([
        [btn("📊 آمار کامل", "a:stats", "primary"), btn("📢 پیام همگانی", "a:bc", "success")],
        [btn("🚫 بن کاربر", "a:ban", "danger"), btn("✅ آنبن کاربر", "a:unban", "success")],
        [btn("🔍 اطلاعات کاربر", "a:info", "primary"), btn("🏆 برترین کاربران", "a:top", "primary")],
        [btn("🧩 ویژگی‌ها", "a:feat", "primary"), btn("📌 جوین اجباری", "a:ch", "success")],
        [btn("🎚 سهمیه روزانه", "a:lim", "primary"), btn("👋 متن خوشامد", "a:wel", "success")],
        [btn("👥 ادمین‌ها", "a:adm", "primary"), btn("📤 خروجی کاربران", "a:exp", "success")],
        [btn("🧾 لاگ درخواست‌ها", "a:log", "primary"),
         btn("🔌 خاموش‌کردن ربات" if on else "🔌 روشن‌کردن ربات", "a:pow", "danger" if on else "success")],
        [btn("🏠 منوی کاربر", "u:main", "primary")],
    ])


def feat_markup():
    rows = []
    for k, label in FEATURES:
        on = get(k) == "1"
        rows.append([btn(f"{label}  {'✅ روشن' if on else '⛔ خاموش'}", f"a:ft:{k}", "success" if on else "danger")])
    mi = int(get("max_images"))
    rows.append([btn("➖", "a:mi:-", "danger"), btn(f"🖼 حداکثر عکس: {mi}", "a:noop", "primary"),
                 btn("➕", "a:mi:+", "success")])
    rows.append([btn("🔙 پنل مدیریت", "a:main", "primary")])
    return InlineKeyboardMarkup(rows)


def user_link(r):
    return f'<a href="tg://user?id={r["id"]}">{esc(r["name"])}</a>'


def user_card(r):
    return (f"👤 <b>{user_link(r)}</b>\n🆔 <code>{r['id']}</code>\n"
            f"🔗 {'@' + esc(r['username']) if r['username'] else '—'}\n"
            f"📅 عضویت: {r['joined']}\n🕒 آخرین بازدید: {r['last_seen']}\n"
            f"💬 پیام‌ها: {r['msgs']}   🧠 تحلیل: {r['photos']}   🖼 عکس: {r['images']}\n"
            f"وضعیت: {'🚫 بن' if r['banned'] else '✅ آزاد'}")


async def admin_cb(update, context, data):
    qy, ud = update.callback_query, context.user_data
    parts = data.split(":")
    cmd = parts[0]

    async def ask(state, text):
        ud["state"] = state
        await edit(qy, text, back_kb("✖️ انصراف"))

    if cmd == "main":
        ud.pop("state", None); ud.pop("bc", None)
        return await edit(qy, admin_home_text(), admin_home_markup())
    if cmd == "noop":
        return
    if cmd == "stats":
        s = stats()
        return await edit(qy, (
            f"📊 <b>آمار کامل</b>\n━━━━━━━━━━━━━━\n"
            f"👥 کل کاربران: <b>{s['users']}</b>\n🆕 عضو جدید امروز: <b>{s['new']}</b>\n"
            f"🟢 فعال امروز: <b>{s['active']}</b>\n🚫 بن‌شده: <b>{s['banned']}</b>\n"
            f"💬 کل پیام‌ها: <b>{s['msgs']}</b>\n🧠 عکس‌های تحلیل‌شده: <b>{s['photos']}</b>\n"
            f"🖼 عکس‌های ارسالی از وب: <b>{s['images']}</b>\n📌 کانال‌های اجباری: <b>{s['channels']}</b>\n"
            f"🎚 سهمیه روزانه: <b>{get('daily_limit')}</b>"), back_kb())
    if cmd == "pow":
        put("bot_on", "0" if get("bot_on") == "1" else "1")
        return await edit(qy, admin_home_text(), admin_home_markup())

    # ── پیام همگانی
    if cmd == "bc":
        return await ask("bc", "📢 <b>پیام همگانی</b>\n\nپیامت رو بفرست (متن، عکس، ویدیو، فایل... هر چی) تا برای همه کاربران ارسال بشه.")
    if cmd == "bc_no":
        ud.pop("bc", None)
        return await edit(qy, "❌ ارسال همگانی لغو شد.", back_kb())
    if cmd == "bc_go":
        bc = ud.pop("bc", None)
        if not bc:
            return await edit(qy, "⚠️ پیامی برای ارسال نیست.", back_kb())
        await edit(qy, "⏳ در حال ارسال...")
        context.application.create_task(do_broadcast(context, bc[0], bc[1], qy.message))
        return

    # ── بن / آنبن / اطلاعات
    if cmd == "ban":
        return await ask("ban", "🚫 <b>بن کاربر</b>\n\nآیدی عددی کاربر رو بفرست:")
    if cmd == "unban":
        return await ask("unban", "✅ <b>آنبن کاربر</b>\n\nآیدی عددی کاربر رو بفرست:")
    if cmd == "info":
        return await ask("info", "🔍 <b>اطلاعات کاربر</b>\n\nآیدی عددی کاربر رو بفرست:")
    if cmd in ("bu", "ub"):
        uid = int(parts[1])
        if cmd == "bu" and is_admin(uid):
            return await qy.answer("نمی‌شه ادمین رو بن کرد", show_alert=True)
        run("UPDATE users SET banned=? WHERE id=?", (1 if cmd == "bu" else 0, uid))
        r = fetch1("SELECT * FROM users WHERE id=?", (uid,))
        return await edit(qy, user_card(r), user_actions(r))
    if cmd == "top":
        rows = fetch("SELECT * FROM users ORDER BY msgs DESC LIMIT 10")
        body = "\n".join(f"{i}. {user_link(r)} — <b>{r['msgs']}</b> پیام" for i, r in enumerate(rows, 1))
        return await edit(qy, "🏆 <b>برترین کاربران</b>\n━━━━━━━━━━━━━━\n" + (body or "—"), back_kb())
    if cmd == "log":
        rows = fetch("SELECT * FROM logs ORDER BY id DESC LIMIT 15")
        icons = {"chat": "💬", "web": "🔎", "img": "🖼", "photo": "🧠"}
        body = "\n".join(f"{icons.get(r['kind'], '•')} <code>{r['uid']}</code>: {esc(r['text'][:45])}" for r in rows)
        return await edit(qy, "🧾 <b>۱۵ درخواست آخر</b>\n━━━━━━━━━━━━━━\n" + (body or "—"), back_kb())

    # ── ویژگی‌ها
    if cmd == "feat":
        return await edit(qy, "🧩 <b>ویژگی‌های ربات</b>\nبرای روشن/خاموش کردن، روی دکمه بزن:", feat_markup())
    if cmd == "ft":
        k = parts[1]
        if k in dict(FEATURES):
            put(k, "0" if get(k) == "1" else "1")
        return await edit(qy, "🧩 <b>ویژگی‌های ربات</b>\nبرای روشن/خاموش کردن، روی دکمه بزن:", feat_markup())
    if cmd == "mi":
        v = int(get("max_images")) + (1 if parts[1] == "+" else -1)
        put("max_images", max(1, min(8, v)))
        return await edit(qy, "🧩 <b>ویژگی‌های ربات</b>\nبرای روشن/خاموش کردن، روی دکمه بزن:", feat_markup())

    # ── جوین اجباری
    if cmd == "ch":
        chans = [r["chat"] for r in fetch("SELECT chat FROM channels")]
        rows = [[btn(f"🗑 حذف {c}", f"a:chdel:{i}", "danger")] for i, c in enumerate(chans)]
        rows.append([btn("➕ افزودن کانال/گروه", "a:chadd", "success")])
        rows.append([btn("🔙 پنل مدیریت", "a:main", "primary")])
        txt = "📌 <b>جوین اجباری</b>\n━━━━━━━━━━━━━━\n" + ("\n".join(f"• {esc(c)}" for c in chans) or "هنوز کانالی اضافه نشده.")
        txt += "\n\n⚠️ ربات باید ادمین کانال باشه."
        return await edit(qy, txt, InlineKeyboardMarkup(rows))
    if cmd == "chadd":
        return await ask("chadd", "📌 یوزرنیم کانال (مثل <code>@mychannel</code>) یا آیدی عددی (<code>-100...</code>) رو بفرست.\nاول ربات رو ادمین کانال کن.")
    if cmd == "chdel":
        chans = [r["chat"] for r in fetch("SELECT chat FROM channels")]
        i = int(parts[1])
        if i < len(chans):
            run("DELETE FROM channels WHERE chat=?", (chans[i],))
        return await admin_cb(update, context, "ch")

    # ── سهمیه و خوشامد
    if cmd == "lim":
        return await ask("lim", f"🎚 <b>سهمیه روزانه</b>\nمقدار فعلی: <b>{get('daily_limit')}</b>\n\nعدد جدید رو بفرست (<code>0</code> = نامحدود):")
    if cmd == "wel":
        return await ask("wel", "👋 <b>متن خوشامد</b>\n\nمتن فعلی:\n━━━━━━━━━━━━━━\n" + get("welcome") +
                         "\n━━━━━━━━━━━━━━\nمتن جدید رو بفرست. از <code>{name}</code> برای اسم کاربر استفاده کن. (فرمت HTML پشتیبانی میشه)")

    # ── ادمین‌ها
    if cmd == "adm":
        extra = [r["id"] for r in fetch("SELECT id FROM admins")]
        rows = [[btn(f"🗑 حذف {a}", f"a:admdel:{a}", "danger")] for a in extra]
        rows.append([btn("➕ افزودن ادمین", "a:admadd", "success")])
        rows.append([btn("🔙 پنل مدیریت", "a:main", "primary")])
        txt = ("👥 <b>ادمین‌ها</b>\n━━━━━━━━━━━━━━\n👑 اصلی: " +
               (", ".join(f"<code>{a}</code>" for a in ENV_ADMINS) or "—") +
               "\n➕ اضافه‌شده: " + (", ".join(f"<code>{a}</code>" for a in extra) or "—"))
        return await edit(qy, txt, InlineKeyboardMarkup(rows))
    if cmd == "admadd":
        return await ask("admadd", "👥 آیدی عددی ادمین جدید رو بفرست:")
    if cmd == "admdel":
        run("DELETE FROM admins WHERE id=?", (int(parts[1]),))
        return await admin_cb(update, context, "adm")

    # ── خروجی
    if cmd == "exp":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "name", "username", "joined", "last_seen", "banned", "msgs", "photos", "images"])
        for r in fetch("SELECT * FROM users"):
            w.writerow([r["id"], r["name"], r["username"], r["joined"], r["last_seen"],
                        r["banned"], r["msgs"], r["photos"], r["images"]])
        data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
        await qy.message.reply_document(data, filename=f"users_{today()}.csv", caption="📤 خروجی کاربران")


def user_actions(r):
    if r["banned"]:
        a = btn("✅ آنبن", f"a:ub:{r['id']}", "success")
    else:
        a = btn("🚫 بن", f"a:bu:{r['id']}", "danger")
    return InlineKeyboardMarkup([[a], [btn("🔙 پنل مدیریت", "a:main", "primary")]])


async def capture_bc(update, context):
    msg = update.effective_message
    context.user_data["bc"] = (msg.chat_id, msg.message_id)
    context.user_data.pop("state", None)
    n = fetch1("SELECT COUNT(*) c FROM users WHERE banned=0")["c"]
    await msg.reply_text(
        f"👆 پیش‌نمایش بالا برای <b>{n}</b> کاربر ارسال بشه؟", parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[btn("✅ ارسال", "a:bc_go", "success"), btn("❌ لغو", "a:bc_no", "danger")]]))


async def do_broadcast(context, chat_id, message_id, report):
    ids = [r["id"] for r in fetch("SELECT id FROM users WHERE banned=0")]
    ok = fail = 0
    for i, uid in enumerate(ids, 1):
        for attempt in range(2):
            try:
                await context.bot.copy_message(uid, chat_id, message_id)
                ok += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except (Forbidden, BadRequest, TelegramError):
                fail += 1
                break
        await asyncio.sleep(0.05)
        if i % 100 == 0:
            try:
                await report.edit_text(f"⏳ ارسال... {i}/{len(ids)}")
            except TelegramError:
                pass
    try:
        await report.edit_text(f"✅ ارسال همگانی تموم شد\n\n📬 موفق: {ok}\n❌ ناموفق: {fail}", reply_markup=back_kb())
    except TelegramError:
        pass


async def admin_input(update, context, state):
    msg, ud = update.effective_message, context.user_data
    text = (msg.text or "").strip()
    num = re.search(r"-?\d+", text)
    done = back_kb()

    if state == "bc":
        return await capture_bc(update, context)
    ud.pop("state", None)

    if state in ("ban", "unban", "info"):
        if not num:
            ud["state"] = state
            return await msg.reply_text("⚠️ آیدی عددی معتبر بفرست.", reply_markup=back_kb("✖️ انصراف"))
        uid = int(num.group())
        r = fetch1("SELECT * FROM users WHERE id=?", (uid,))
        if not r:
            return await msg.reply_text("❌ این کاربر تو دیتابیس نیست.", reply_markup=done)
        if state == "ban" and is_admin(uid):
            return await msg.reply_text("⛔ نمی‌شه ادمین رو بن کرد.", reply_markup=done)
        if state in ("ban", "unban"):
            run("UPDATE users SET banned=? WHERE id=?", (1 if state == "ban" else 0, uid))
            r = fetch1("SELECT * FROM users WHERE id=?", (uid,))
        return await msg.reply_text(user_card(r), parse_mode=ParseMode.HTML, reply_markup=user_actions(r))

    if state == "lim":
        if not num or int(num.group()) < 0:
            ud["state"] = state
            return await msg.reply_text("⚠️ یه عدد (۰ یا بیشتر) بفرست.", reply_markup=back_kb("✖️ انصراف"))
        put("daily_limit", int(num.group()))
        return await msg.reply_text(f"✅ سهمیه روزانه روی <b>{int(num.group())}</b> تنظیم شد.",
                                    parse_mode=ParseMode.HTML, reply_markup=done)
    if state == "wel":
        put("welcome", msg.text_html or text)
        return await msg.reply_text("✅ متن خوشامد ذخیره شد.", reply_markup=done)
    if state == "admadd":
        if not num:
            ud["state"] = state
            return await msg.reply_text("⚠️ آیدی عددی معتبر بفرست.", reply_markup=back_kb("✖️ انصراف"))
        run("INSERT OR IGNORE INTO admins(id) VALUES(?)", (int(num.group()),))
        return await msg.reply_text("✅ ادمین اضافه شد.", reply_markup=done)
    if state == "chadd":
        ch = text.replace("https://t.me/", "@").replace("t.me/", "@")
        if not (ch.startswith("@") or ch.lstrip("-").isdigit()):
            ud["state"] = state
            return await msg.reply_text("⚠️ فرمت درست: <code>@username</code> یا <code>-100...</code>",
                                        parse_mode=ParseMode.HTML, reply_markup=back_kb("✖️ انصراف"))
        try:
            m = await context.bot.get_chat_member(ch, context.bot.id)
            if m.status not in ("administrator", "creator"):
                return await msg.reply_text("⚠️ ربات تو اون کانال ادمین نیست. اول ادمینش کن.", reply_markup=done)
        except TelegramError as e:
            return await msg.reply_text(f"❌ کانال پیدا نشد یا ربات دسترسی نداره.\n<code>{esc(e)}</code>",
                                        parse_mode=ParseMode.HTML, reply_markup=done)
        run("INSERT OR IGNORE INTO channels(chat) VALUES(?)", (ch,))
        return await msg.reply_text(f"✅ {esc(ch)} به جوین اجباری اضافه شد.", parse_mode=ParseMode.HTML, reply_markup=done)


async def on_error(update, context):
    LOG.error("Unhandled error", exc_info=context.error)


async def post_init(app):
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start", "شروع / منوی اصلی"),
        BotCommand("help", "راهنما"),
        BotCommand("panel", "پنل مدیریت (ادمین)"),
    ])


def main():
    if not ENV_ADMINS:
        LOG.warning("ADMIN_IDS تنظیم نشده؛ هیچ‌کس به پنل مدیریت دسترسی نخواهد داشت!")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler(["panel", "admin"], cmd_panel))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.TEXT & ~filters.PHOTO & ~filters.COMMAND & ~filters.StatusUpdate.ALL, on_other))
    app.add_error_handler(on_error)
    LOG.info("✅ ربات روشن شد")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
