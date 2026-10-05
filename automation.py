"""
Automation Pro v3 - Compact + Engagement Edition

Naya kya hai (purane features sab as-is: AI chat, auto-reply, moderation,
welcome, broadcast, schedule, persistent DB):
  * Daily reward + streak + XP/levels + leaderboard   (/daily /me /top)
  * Games: quiz, paheli, dice, slot, darts, guess      (/games)
  * AI modes: dost / teacher / coder / shayar / coach  (/mode)
  * Personal reminders                                 (/remind /reminders)
  * Referral rewards                                   (/invite)
  * Subah + shaam smart notifications (opt-out: /notify off)
  * Admin: /backup + roz raat 3 baje auto-backup admin ko

Install:
    pip install "python-telegram-bot[job-queue]>=21" "openai>=1.60" python-dotenv tzdata

.env:
    TELEGRAM_BOT_TOKEN=123456:ABC...
    OPENAI_API_KEY=sk-...        (optional - bina iske quiz/paheli built-in chalte hain)
    OPENAI_MODEL=gpt-4.1-mini    (optional)
    ADMIN_ID=123456789
    BOT_NAME=Automation Pro
    TIMEZONE=Asia/Kolkata
    MORNING_HOUR=8               (optional)
    EVENING_HOUR=20              (optional)

Termux: pehli baar `termux-setup-storage`, phir `python automation.py`
"""

import asyncio
import functools
import json
import logging
import os
import random
import re
import sqlite3
import time as pytime
from contextlib import closing
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ==================================================
# CONFIG
# ==================================================

load_dotenv()
env = os.getenv


def to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


TOKEN = env("TELEGRAM_BOT_TOKEN")
OPENAI_KEY = env("OPENAI_API_KEY")
MODEL = env("OPENAI_MODEL", "gpt-4.1-mini")
ADMIN_ID = to_int(env("ADMIN_ID"))
BOT_NAME = env("BOT_NAME", "Automation Pro")
MORNING_HOUR = to_int(env("MORNING_HOUR"), 8)
EVENING_HOUR = to_int(env("EVENING_HOUR"), 20)
try:
    TZ = ZoneInfo(env("TIMEZONE", "Asia/Kolkata"))
except Exception:
    TZ = ZoneInfo("Asia/Kolkata")

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN missing in .env")

AI_COOLDOWN = 3        # seconds between AI calls per user
HISTORY_LIMIT = 10     # AI memory per user
GAME_CAP = 300         # max game coins per user per day
PUSH_DAYS = 7          # notifications only to users active in last N days
MAX_REMINDERS = 10
SETTING_DEFAULTS = {"ai": "on", "autoreply": "off", "moderation": "off", "welcome": "off"}

# Persistent DB: Android shared storage survives Termux reinstall.
PERSIST_DIR = os.path.expanduser("~/storage/shared/AutomationPro")
LOCAL_DB = os.path.expanduser("~/automation.db")


def pick_db():
    try:
        os.makedirs(PERSIST_DIR, exist_ok=True)
        probe = os.path.join(PERSIST_DIR, ".write_test")
        open(probe, "w").close()
        os.remove(probe)
        return os.path.join(PERSIST_DIR, "automation.db"), True
    except OSError as exc:
        print(f"WARNING: shared storage unavailable ({exc}). Run: termux-setup-storage")
        return LOCAL_DB, False


DB_PATH = 'automation.db'

PERSISTENT = False

client = AsyncOpenAI(api_key=OPENAI_KEY) if OPENAI_KEY else None

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

HISTORY: dict[int, list] = {}
LAST_AI: dict[int, float] = {}
EARN: dict[int, tuple] = {}   # uid -> (date, coins earned today from games)
TIP = {"date": None, "text": ""}

MODES = {
    "friend": ("😎 Dost", "Be a friendly, witty buddy. Keep replies short and casual."),
    "tutor": ("📚 Teacher", "Be a patient teacher. Explain step by step with a simple example, end with one tiny practice question."),
    "coder": ("💻 Coder", "Be an expert programmer. Give working code and a brief explanation."),
    "shayar": ("🌹 Shayar", "Reply like a poetic Hindi/Urdu shayar, in short shayari style."),
    "coach": ("💪 Coach", "Be a motivating life/study coach. Give exactly one actionable next step."),
}

QUIZ_BANK = [
    ("Bharat ki rajdhani kya hai?", "New Delhi", ["Mumbai", "Kolkata", "Chennai"]),
    ("Sabse bada grah kaunsa hai?", "Jupiter", ["Saturn", "Mars", "Earth"]),
    ("Python ko kisne banaya?", "Guido van Rossum", ["Linus Torvalds", "Elon Musk", "Bill Gates"]),
    ("Taj Mahal kis shehar me hai?", "Agra", ["Jaipur", "Lucknow", "Delhi"]),
    ("Paani ka chemical formula?", "H2O", ["CO2", "O2", "NaCl"]),
    ("Telegram kis saal launch hua?", "2013", ["2008", "2016", "2010"]),
    ("Insaan ke sharir me kitni haddiyan hoti hain (adult)?", "206", ["108", "300", "150"]),
    ("Surya kis disha me ugta hai?", "Poorab", ["Paschim", "Uttar", "Dakshin"]),
]
RIDDLES = [
    ("Mere paas shehar hain par ghar nahi, jungle hain par ped nahi. Main kaun?", "Naksha (Map) 🗺️"),
    ("Jitna kharch karo utna badhta hai. Kya?", "Gyaan 📚"),
    ("Hamesha aata hai par kabhi pahunchta nahi. Kya?", "Kal (Tomorrow) ⏳"),
    ("Dhoop me aata hai, chhaon me khoya jaata hai. Kya?", "Parchhaai (Shadow)"),
]
TIPS = [
    "Chhoti daily aadat badi success banati hai. Aaj bas ek kaam poora karo! 💪",
    "Fun fact: shahad kabhi kharab nahi hota. 🍯",
    "Jo seekhna band kar de, wo purana ho jaata hai. Aaj kuch naya seekho! 📚",
    "Fun fact: octopus ke 3 dil hote hain. 🐙",
]

# ==================================================
# DATABASE
# ==================================================


def run(sql, params=(), fetch=None):
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as con:
        con.execute("PRAGMA busy_timeout=30000")
        cur = con.execute(sql, params)
        con.commit()
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        return cur


SCHEMA = [
    "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined_at TEXT)",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS auto_replies (keyword TEXT PRIMARY KEY, response TEXT)",
    "CREATE TABLE IF NOT EXISTS banned_words (word TEXT PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, value INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS schedules (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, hh INTEGER NOT NULL, mm INTEGER NOT NULL, message TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, due REAL NOT NULL, text TEXT NOT NULL)",
]
NEW_COLS = {  # auto-migrated into your existing users table (old data safe)
    "coins": "INTEGER DEFAULT 0", "xp": "INTEGER DEFAULT 0", "streak": "INTEGER DEFAULT 0",
    "last_daily": "TEXT DEFAULT ''", "last_seen": "REAL DEFAULT 0", "notify": "INTEGER DEFAULT 1",
    "mode": "TEXT DEFAULT 'friend'", "ref_by": "INTEGER DEFAULT 0", "wins": "INTEGER DEFAULT 0",
}


def init_db():
    for stmt in SCHEMA:
        run(stmt)
    have = {r[1] for r in run("PRAGMA table_info(users)", fetch="all")}
    for col, ddl in NEW_COLS.items():
        if col not in have:
            run(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
    logger.info("DB ready: %s (persistent=%s)", DB_PATH, PERSISTENT)


def get_setting(key):
    row = run("SELECT value FROM settings WHERE key=?", (key,), "one")
    return row[0] if row else SETTING_DEFAULTS.get(key, "off")


def set_setting(key, value):
    run("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def increment_stat(key):
    run("INSERT INTO stats(key, value) VALUES (?, 1) ON CONFLICT(key) DO UPDATE SET value = value + 1", (key,))


def get_stat(key):
    row = run("SELECT value FROM stats WHERE key=?", (key,), "one")
    return row[0] if row else 0


def snapshot():
    path = os.path.expanduser("~/backup_snapshot.db")
    with closing(sqlite3.connect(DB_PATH)) as src, closing(sqlite3.connect(path)) as dst:
        src.backup(dst)
    return path


# ==================================================
# USERS / REWARDS
# ==================================================


def today():
    return datetime.now(TZ).date()


def level(xp):
    return int((xp / 20) ** 0.5) + 1


def save_user(user):
    new = run("SELECT 1 FROM users WHERE user_id=?", (user.id,), "one") is None
    run(
        "INSERT INTO users (user_id, username, first_name, joined_at, last_seen) VALUES (?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
        "first_name=excluded.first_name, last_seen=excluded.last_seen",
        (user.id, user.username or "", user.first_name or "",
         datetime.now(timezone.utc).isoformat(), pytime.time()),
    )
    return new


def profile(uid):
    row = run("SELECT coins, xp, streak, last_daily, notify, mode, wins FROM users WHERE user_id=?", (uid,), "one")
    return row or (0, 0, 0, "", 1, "friend", 0)


def reward(uid, coins=0, xp=0):
    """Add coins/xp. Returns True if the user just levelled up."""
    row = run("SELECT xp FROM users WHERE user_id=?", (uid,), "one")
    if not row:
        return False
    run("UPDATE users SET coins=coins+?, xp=xp+? WHERE user_id=?", (coins, xp, uid))
    return level(row[0] + xp) > level(row[0])


def game_reward(uid, coins, xp):
    """Daily-capped game reward -> (levelled_up, coins_given)."""
    d, earned = EARN.get(uid, (today(), 0))
    if d != today():
        earned = 0
    coins = max(0, min(coins, GAME_CAP - earned))
    EARN[uid] = (today(), earned + coins)
    increment_stat("games")
    return reward(uid, coins, xp), coins


# ==================================================
# HELPERS
# ==================================================


def kb(*rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


NEXT_KB = kb([("🎁 Daily", "go:daily"), ("🎮 Games", "go:games")], [("🏠 Menu", "go:menu")])
AI_KB = kb([("➕ Aur detail", "ai:more"), ("✂️ Short me", "ai:short")],
           [("🎮 Games", "go:games"), ("🎁 Daily", "go:daily")], [("🏠 Menu", "go:menu")])
MAIN_KB = ReplyKeyboardMarkup([["🏠 Menu"]], resize_keyboard=True, is_persistent=True)  # hamesha neeche rehta hai
ADMIN_SAFE = {"panel", "stats", "dbinfo", "backup", "replies", "schedules"}


def menu_markup(uid):
    rows = [
        [("🎁 Daily", "go:daily"), ("🎮 Games", "go:games")],
        [("🧠 Quiz", "go:quiz"), ("🧩 Paheli", "go:riddle")],
        [("👤 Profile", "go:me"), ("🏆 Top", "go:top")],
        [("🎭 AI Mode", "go:mode"), ("🔗 Invite", "go:invite")],
        [("⏰ Reminders", "go:reminders"), ("📚 Help", "go:help")],
    ]
    if is_admin(uid):
        rows += [
            [("⚙️ Admin Panel", "ad:panel"), ("📊 Stats", "ad:stats")],
            [("💾 Backup", "ad:backup"), ("🗄️ DB Info", "ad:dbinfo")],
            [("📝 Replies", "ad:replies"), ("⏰ Schedules", "ad:schedules")],
            [("📢 Broadcast", "broadcast_help")],
        ]
    return kb(*rows)
UP = "\n🎉 LEVEL UP!"
FOOT = "\n\n🔕 Band karne ke liye: /notify off"


def say(u, text, **kw):
    return u.effective_message.reply_text(text, **kw)


def arg(u):
    return (u.effective_message.text or "").partition(" ")[2].strip()


def human(secs):
    h, m = divmod(int(secs) // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def is_admin(uid):
    return ADMIN_ID != 0 and uid == ADMIN_ID


async def admin_only(u):
    user = u.effective_user
    if user and is_admin(user.id):
        return True
    if u.callback_query:
        await u.callback_query.answer("⛔ Admin only", show_alert=True)
    elif u.message:
        await u.message.reply_text("⛔ यह command केवल admin के लिए है।")
    return False


def admin(fn):
    @functools.wraps(fn)
    async def wrapper(u, c):
        if await admin_only(u):
            return await fn(u, c)
    return wrapper


async def need_private(u):
    if u.effective_chat.type != "private":
        await say(u, "🎮 यह feature private chat में है — मुझे DM करें।")
        return False
    save_user(u.effective_user)
    return True


async def reply_long(msg, text, markup=None):
    text = text or "..."
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    for i, chunk in enumerate(chunks):
        await msg.reply_text(chunk, reply_markup=markup if i == len(chunks) - 1 else None)


def keyword_matches(keyword, text):
    keyword, text = keyword.lower().strip(), text.lower()
    if not keyword:
        return False
    if keyword.isascii():
        return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text) is not None
    return keyword in text


def find_auto_reply(text):
    for keyword, response in run("SELECT keyword, response FROM auto_replies", fetch="all"):
        if keyword_matches(keyword, text):
            return response


def contains_banned_word(text):
    return any(keyword_matches(w, text) for (w,) in run("SELECT word FROM banned_words", fetch="all"))


# ==================================================
# AI
# ==================================================


async def ai_once(prompt):
    if not client:
        return None
    try:
        return (await client.responses.create(model=MODEL, input=prompt)).output_text
    except Exception:
        logger.warning("ai_once failed", exc_info=True)


async def ai_chat(uid, text):
    if not client:
        return "⚠️ OpenAI API key configured नहीं है।"
    mode = profile(uid)[5]
    history = HISTORY.setdefault(uid, [])
    history.append({"role": "user", "content": text})
    del history[:-HISTORY_LIMIT]
    try:
        resp = await client.responses.create(
            model=MODEL,
            instructions=(
                f"You are {BOT_NAME}, a helpful Telegram AI assistant. Reply in the same language "
                f"the user writes in (Hindi/English/Hinglish). Be concise. {MODES.get(mode, MODES['friend'])[1]}"
            ),
            input=history,
        )
    except Exception:
        history.pop()
        raise
    answer = resp.output_text or "..."
    history.append({"role": "assistant", "content": answer})
    del history[:-HISTORY_LIMIT]
    return answer


def cooling(uid):
    now = pytime.monotonic()
    if now - LAST_AI.get(uid, 0) < AI_COOLDOWN:
        return True
    LAST_AI[uid] = now
    return False


def should_ai_reply(u, c, text):
    if u.effective_chat.type == "private":
        return text
    bot_username = (c.bot.username or "").lower()
    r = u.message.reply_to_message
    replied = r and r.from_user and r.from_user.id == c.bot.id
    mentioned = bot_username and f"@{bot_username}" in text.lower()
    if not (replied or mentioned):
        return None
    if mentioned:
        text = re.sub(f"@{re.escape(bot_username)}", "", text, flags=re.I).strip()
    return text or None


async def ai_reply(u, c, text):
    """Shared by messages and follow-up buttons."""
    uid, chat = u.effective_user.id, u.effective_chat
    if cooling(uid):
        return await say(u, "⏳ थोड़ा रुकिए, फिर से भेजें।")
    try:
        await chat.send_action(ChatAction.TYPING)
        answer = await ai_chat(uid, text)
        private = chat.type == "private"
        if private:
            reward(uid, 0, 1)
        increment_stat("ai_replies")
        await reply_long(u.effective_message, answer, AI_KB if private else None)
    except Exception:
        logger.exception("AI error")
        await say(u, "⚠️ AI service में समस्या आई।")


async def make_quiz():
    t = await ai_once(
        'Give ONE fun general-knowledge MCQ (India/world/science/tech) in Hinglish as JSON only: '
        '{"q": str, "o": [4 short strings], "a": index 0-3}'
    )
    try:
        d = json.loads(re.search(r"\{.*\}", t, re.S).group())
        if len(d["o"]) == 4 and d["a"] in range(4):
            return d["q"], d["o"], d["a"]
    except Exception:
        pass
    q, right, wrong = random.choice(QUIZ_BANK)
    opts = wrong + [right]
    random.shuffle(opts)
    return q, opts, opts.index(right)


async def daily_tip():
    if TIP["date"] != today():
        TIP["date"] = today()
        TIP["text"] = (await ai_once("Ek chhota motivational vichar ya interesting fact Hinglish me do. Max 2 lines.")
                       or random.choice(TIPS)).strip()
    return TIP["text"]


# ==================================================
# START / HELP / BASIC
# ==================================================

HELP_TEXT = (
    "📚 Commands\n\n"
    "🏠 /menu - Main menu (buttons)\n"
    "🎁 /daily - Roz ka reward + streak\n"
    "🎮 /games - Quiz, dice, slot, paheli...\n"
    "👤 /me - Profile & level\n"
    "🏆 /top - Leaderboard\n"
    "🎭 /mode - AI ka style badlo\n"
    "🔗 /invite - Dost invite karo, coins jeeto\n"
    "⏰ /remind 30m | kaam   (m/h/d)\n"
    "📋 /reminders  ·  /delremind id\n"
    "🔔 /notify on/off\n"
    "🆔 /id  ·  🧹 /clear (AI memory)\n\n"
    "🔐 Admin:\n"
    "/panel /stats /dbinfo /backup\n"
    "/broadcast msg\n"
    "/addreply k | r  ·  /delreply k  ·  /replies\n"
    "/addban w  ·  /delban w\n"
    "/ai /autoreply /moderation /welcome  on|off\n"
    "/schedule HH:MM | msg  ·  /schedules  ·  /delschedule id\n\n"
    "💬 Seedha kuch bhi likho — AI jawab dega.\n"
    "(Group me @mention karo ya bot ke message par reply karo)"
)


async def start(u, c):
    user = u.effective_user
    increment_stat("messages")
    if u.effective_chat.type != "private":
        return await say(u, f"👋 Main {BOT_NAME} hu. Mujhe @mention karo ya mere message par reply karo.")
    new, extra = save_user(user), ""
    if new:
        reward(user.id, 50, 0)
        extra = "\n🎁 Welcome bonus: +50 🪙"
        ref = to_int(c.args[0][4:]) if c.args and c.args[0].startswith("ref_") else 0
        if ref and ref != user.id and run("SELECT 1 FROM users WHERE user_id=?", (ref,), "one"):
            run("UPDATE users SET ref_by=? WHERE user_id=?", (ref, user.id))
            reward(ref, 100, 20)
            reward(user.id, 50, 0)
            extra += "\n🤝 Invite bonus: +50 🪙"
            try:
                await c.bot.send_message(ref, f"🎉 {user.first_name} aapke link se join hua! +100 🪙")
            except Exception:
                pass
    coins, xp, streak, last, *_ = profile(user.id)
    todo = "" if last == today().isoformat() else "\n🎁 Aaj ka daily reward claim karna baaki hai!"
    await say(
        u,
        f"👋 {'Welcome' if new else 'Welcome back'} {user.first_name}!{extra}\n\n"
        f"🤖 {BOT_NAME} — AI + Games + Rewards\n🪙 {coins}  ·  🔥 Streak {streak}{todo}\n\n"
        "💬 Kuch bhi poochho, ya neeche menu use karo.",
        reply_markup=MAIN_KB,
    )
    await menu(u, c)


async def help_command(u, c):
    await say(u, HELP_TEXT)


async def menu(u, c):
    uid = u.effective_user.id
    await say(u, "🏠 MAIN MENU" + (" (Admin)" if is_admin(uid) else "") + "\n\nNeeche se chuno 👇",
              reply_markup=menu_markup(uid))


async def show_id(u, c):
    await say(
        u,
        f"👤 User ID: <code>{u.effective_user.id}</code>\n💬 Chat ID: <code>{u.effective_chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def clear_memory(u, c):
    HISTORY.pop(u.effective_user.id, None)
    await say(u, "🧹 आपकी AI chat memory clear हो गई।")


# ==================================================
# ENGAGEMENT: DAILY / PROFILE / TOP / MODE / INVITE / NOTIFY
# ==================================================


async def daily(u, c):
    if not await need_private(u):
        return
    uid, t = u.effective_user.id, today()
    _, _, streak, last, *_ = profile(uid)
    if last == t.isoformat():
        left = datetime.combine(t + timedelta(days=1), time(), TZ) - datetime.now(TZ)
        return await say(
            u, f"✅ Aaj ka reward le liya!\n🔥 Streak: {streak} din\n⏳ Agla reward: {human(left.total_seconds())} baad",
            reply_markup=kb([("🧠 Quiz", "go:quiz"), ("🎮 Games", "go:games")]),
        )
    streak = streak + 1 if last == (t - timedelta(days=1)).isoformat() else 1
    weekly = 100 if streak % 7 == 0 else 0
    bonus = 20 + min(streak, 7) * 5 + weekly
    run("UPDATE users SET streak=?, last_daily=? WHERE user_id=?", (streak, t.isoformat(), uid))
    up = reward(uid, bonus, 10)
    await say(
        u,
        f"🎁 Daily reward: +{bonus} 🪙 (+10 XP)\n🔥 Streak: {streak} din"
        + ("\n🏅 Weekly streak bonus +100!" if weekly else "\n📅 Kal wapas aao, streak mat todna!")
        + (UP if up else ""),
        reply_markup=kb([("🧠 Quiz khelo", "go:quiz"), ("🎮 Games", "go:games")]),
    )


async def me(u, c):
    if not await need_private(u):
        return
    uid = u.effective_user.id
    coins, xp, streak, _, notify_on, mode, wins = profile(uid)
    lv = level(xp)
    lo, hi = 20 * (lv - 1) ** 2, 20 * lv ** 2
    k = int(10 * (xp - lo) / (hi - lo))
    rank = run("SELECT COUNT(*)+1 FROM users WHERE xp > ?", (xp,), "one")[0]
    await say(
        u,
        f"👤 {u.effective_user.first_name}\n\n"
        f"⭐ Level {lv}\n{'█' * k}{'░' * (10 - k)} {xp}/{hi} XP\n"
        f"🪙 Coins: {coins}\n🔥 Streak: {streak} din\n🏆 Rank: #{rank}\n🧠 Quiz jeete: {wins}\n"
        f"🎭 Mode: {MODES.get(mode, MODES['friend'])[0]}\n🔔 Notifications: {'ON' if notify_on else 'OFF'}",
        reply_markup=kb([("🔔 Notifications badlo", "nt"), ("🎭 Mode", "go:mode")]),
    )


async def top(u, c):
    rows = run("SELECT first_name, xp, streak FROM users ORDER BY xp DESC LIMIT 10", fetch="all")
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else str(i + 1) + '.'} {n or 'User'} — Lv{level(x)} · {x} XP · 🔥{s}"
        for i, (n, x, s) in enumerate(rows)
    ]
    await say(u, "🏆 LEADERBOARD\n\n" + ("\n".join(lines) or "Abhi koi nahi — pehle aap bano! 😎"), reply_markup=NEXT_KB)


async def mode_cmd(u, c):
    if not await need_private(u):
        return
    cur = profile(u.effective_user.id)[5]
    await say(u, "🎭 AI ka style chuno:", reply_markup=kb(
        *[[(label + (" ✅" if key == cur else ""), f"md:{key}")] for key, (label, _) in MODES.items()]))


async def invite(u, c):
    if not await need_private(u):
        return
    uid = u.effective_user.id
    n = run("SELECT COUNT(*) FROM users WHERE ref_by=?", (uid,), "one")[0]
    await say(
        u,
        "🔗 Dost ko invite karo!\nAapko +100 🪙, dost ko +50 🪙\n\n"
        f"https://t.me/{c.bot.username}?start=ref_{uid}\n\n👥 Aapke invites: {n}",
    )


async def notify(u, c):
    if not await need_private(u):
        return
    uid = u.effective_user.id
    v = {"on": 1, "off": 0}.get(c.args[0].lower()) if c.args else None
    if v is None:
        v = 1 - profile(uid)[4]
    run("UPDATE users SET notify=? WHERE user_id=?", (v, uid))
    await say(u, f"🔔 Notifications {'ON ✅' if v else 'OFF 🔕'}")


# ==================================================
# GAMES
# ==================================================


async def games(u, c):
    await say(
        u, "🎮 GAME ZONE\nKhelo, coins aur XP jeeto! 🪙",
        reply_markup=kb([("🧠 Quiz", "go:quiz"), ("🧩 Paheli", "go:riddle")],
                        [("🎲 Dice", "go:dice"), ("🎰 Slot", "go:slot")],
                        [("🎯 Darts", "go:darts"), ("🔢 Guess", "go:guess")]),
    )


async def quiz(u, c):
    if not await need_private(u):
        return
    await u.effective_chat.send_action(ChatAction.TYPING)
    q, opts, ans = await make_quiz()
    qid = random.randint(1000, 9999)
    c.user_data["quiz"] = (qid, ans, opts)
    await say(u, f"🧠 QUIZ\n\n{q}", reply_markup=kb(*[[(o, f"qz:{qid}:{i}")] for i, o in enumerate(opts)]))


async def quiz_answer(u, c, qid, chosen):
    cq, st = u.callback_query, c.user_data.get("quiz")
    if not st or str(st[0]) != qid:
        return await cq.answer("⌛ Ye quiz khatam ho chuka.", show_alert=True)
    c.user_data.pop("quiz")
    await cq.answer()
    await cq.edit_message_reply_markup(reply_markup=None)
    uid, ok = u.effective_user.id, int(chosen) == st[1]
    if ok:
        run("UPDATE users SET wins=wins+1 WHERE user_id=?", (uid,))
    up, coins = game_reward(uid, 15 if ok else 0, 8 if ok else 2)
    text = (f"✅ Sahi jawab! +{coins} 🪙" if ok else f"❌ Galat! Sahi jawab: {st[2][st[1]]}")
    if ok and coins == 0:
        text += " (aaj ka coin limit poora, XP mila)"
    await say(u, text + (UP if up else ""), reply_markup=kb([("🧠 Next Quiz", "go:quiz"), ("🎮 Games", "go:games")]))


async def riddle(u, c):
    if not await need_private(u):
        return
    t = await ai_once("Ek nayi chhoti paheli Hinglish me do. Sirf is format me: Q: <paheli> || A: <jawab>")
    m = re.search(r"Q:\s*(.+?)\s*\|\|\s*A:\s*(.+)", t or "", re.S)
    q, a = (m[1], m[2]) if m else random.choice(RIDDLES)
    c.user_data["riddle"] = a.strip()
    await say(u, f"🧩 PAHELI\n\n{q.strip()}", reply_markup=kb([("💡 Jawab dekho", "rd")], [("🧩 Next", "go:riddle"), ("🎮 Games", "go:games")]))


def dice_game(emoji, payout):
    async def handler(u, c):
        if not await need_private(u):
            return
        msg = await u.effective_message.reply_dice(emoji=emoji)
        await asyncio.sleep(4)  # animation finish hone do
        up, coins = game_reward(u.effective_user.id, payout(msg.dice.value), 3)
        await say(u, f"{emoji} Result: {msg.dice.value}\n🪙 +{coins}" + (UP if up else ""),
                  reply_markup=kb([(f"{emoji} Phir se", f"go:{'dice' if emoji == '🎲' else 'slot' if emoji == '🎰' else 'darts'}"), ("🎮 Games", "go:games")]))
    return handler


dice = dice_game("🎲", lambda v: 15 if v == 6 else 3)
darts = dice_game("🎯", lambda v: 20 if v == 6 else 8 if v >= 4 else 2)
slot = dice_game("🎰", lambda v: 100 if v == 64 else 40 if v in (1, 22, 43) else 2)


async def guess_start(u, c):
    if not await need_private(u):
        return
    c.user_data["guess"] = [random.randint(1, 50), 6]
    await say(u, "🔢 Maine 1 se 50 ke beech ek number socha hai.\n6 mauke hain — number bhejo!")


async def guess_play(u, c, n):
    g = c.user_data["guess"]
    g[1] -= 1
    if n == g[0]:
        c.user_data.pop("guess")
        up, coins = game_reward(u.effective_user.id, 10 + g[1] * 5, 6)
        return await say(u, f"🎉 Sahi! Number {n} tha. +{coins} 🪙" + (UP if up else ""),
                         reply_markup=kb([("🔢 Phir se", "go:guess"), ("🎮 Games", "go:games")]))
    if g[1] <= 0:
        c.user_data.pop("guess")
        return await say(u, f"😅 Mauke khatam! Number {g[0]} tha.", reply_markup=kb([("🔢 Phir se", "go:guess")]))
    await say(u, f"{'⬆️ Aur bada' if n < g[0] else '⬇️ Aur chhota'} — {g[1]} mauke bache")


# ==================================================
# REMINDERS
# ==================================================


async def fire_reminder(c):
    rid = c.job.data
    row = run("SELECT user_id, text FROM reminders WHERE id=?", (rid,), "one")
    if not row:
        return
    run("DELETE FROM reminders WHERE id=?", (rid,))
    try:
        await c.bot.send_message(row[0], f"⏰ Reminder:\n{row[1]}")
    except Exception:
        logger.warning("Reminder %s failed", rid)


async def remind(u, c):
    if not await need_private(u):
        return
    jq, uid = c.application.job_queue, u.effective_user.id
    m = re.match(r"^\s*(\d+)\s*([mhd])\s*\|?\s*(.+)$", arg(u), re.S | re.I)
    if not m:
        return await say(u, "Usage:\n/remind 30m | paani peena\n/remind 2h | meeting\n(m = minute, h = ghanta, d = din)")
    secs = int(m[1]) * {"m": 60, "h": 3600, "d": 86400}[m[2].lower()]
    if jq is None or not 0 < secs <= 30 * 86400:
        return await say(u, "❌ Time 1 minute se 30 din ke beech do.")
    if run("SELECT COUNT(*) FROM reminders WHERE user_id=?", (uid,), "one")[0] >= MAX_REMINDERS:
        return await say(u, f"❌ Max {MAX_REMINDERS} reminders. /reminders se purane hatao.")
    cur = run("INSERT INTO reminders (user_id, due, text) VALUES (?,?,?)", (uid, pytime.time() + secs, m[3].strip()))
    jq.run_once(fire_reminder, secs, data=cur.lastrowid, name=f"rem_{cur.lastrowid}")
    await say(u, f"⏰ Reminder #{cur.lastrowid} set — {human(secs)} baad.")


async def reminders(u, c):
    rows = run("SELECT id, due, text FROM reminders WHERE user_id=? ORDER BY due", (u.effective_user.id,), "all")
    now = pytime.time()
    await say(u, "⏰ REMINDERS\n\n" + "\n".join(f"#{i} — {human(max(d - now, 0))} baad — {t[:50]}" for i, d, t in rows)
              if rows else "Koi reminder nahi. /remind 30m | kaam")


async def delremind(u, c):
    if not c.args or not c.args[0].isdigit():
        return await say(u, "Usage: /delremind id")
    rid = int(c.args[0])
    if not run("DELETE FROM reminders WHERE id=? AND user_id=?", (rid, u.effective_user.id)).rowcount:
        return await say(u, "❌ Reminder nahi mila.")
    for job in c.application.job_queue.get_jobs_by_name(f"rem_{rid}"):
        job.schedule_removal()
    await say(u, "✅ Reminder delete.")


# ==================================================
# SMART PUSH (subah + shaam) & BACKUP
# ==================================================


async def push_job(c):
    slot, t = c.job.data, today().isoformat()
    cutoff = pytime.time() - PUSH_DAYS * 86400
    rows = run("SELECT user_id, first_name, streak, last_daily FROM users WHERE notify=1 AND last_seen>?", (cutoff,), "all")
    tip = await daily_tip() if slot == "m" else ""
    sent = 0
    for uid, name, streak, last in rows:
        name = name or "dost"
        if slot == "e":
            if last == t:
                continue
            text = (f"⚠️ {name}, aapki 🔥 {streak} din ki streak aaj toot sakti hai! Abhi claim karo."
                    if streak else "🎁 Aaj ka reward abhi claim nahi hua — 10 second ka kaam hai!")
        else:
            text = (f"🌅 Good morning {name}!\n\n💡 {tip}\n\n"
                    + (f"🔥 Streak {streak} din — aaj ka 🎁 reward claim karo!" if streak else "🎁 Aaj ka daily reward ready hai!"))
        try:
            await c.bot.send_message(uid, text + FOOT, reply_markup=kb([("🎁 Claim", "go:daily"), ("🎮 Play", "go:games")]))
            sent += 1
        except Forbidden:
            run("UPDATE users SET notify=0 WHERE user_id=?", (uid,))
        except Exception:
            logger.warning("Push to %s failed", uid)
        await asyncio.sleep(0.05)
    logger.info("Push '%s' sent to %d users", slot, sent)


async def auto_backup(c):
    if not ADMIN_ID:
        return
    try:
        with open(snapshot(), "rb") as f:
            await c.bot.send_document(ADMIN_ID, f, filename=f"automation_{today()}.db", caption="💾 Daily DB backup")
    except Exception:
        logger.exception("Auto backup failed")


# ==================================================
# MESSAGE HANDLER
# ==================================================


async def message_handler(u, c):
    msg = u.message
    if not msg or not msg.text:
        return
    user, chat, text = u.effective_user, u.effective_chat, msg.text
    private = chat.type == "private"
    if private:
        save_user(user)
    increment_stat("messages")

    if (chat.type in ("group", "supergroup") and not is_admin(user.id)
            and get_setting("moderation") == "on" and contains_banned_word(text)):
        try:
            await msg.delete()
            increment_stat("deleted_messages")
        except Exception:
            logger.warning("Could not delete message.")
        return

    if private:
        if text in MENU:
            return await MENU[text](u, c)
        if c.user_data.get("guess") and text.strip().isdigit():
            return await guess_play(u, c, int(text))

    if get_setting("autoreply") == "on":
        reply = find_auto_reply(text)
        if reply:
            return await msg.reply_text(reply)

    if get_setting("ai") != "on":
        return
    ai_text = should_ai_reply(u, c, text)
    if ai_text:
        await ai_reply(u, c, ai_text)


# ==================================================
# ADMIN
# ==================================================


def panel_text():
    return ("⚙️ ADMIN DASHBOARD\n\n" + "\n".join(f"{ic} {k}: {get_setting(k)}" for ic, k in
            (("🤖", "ai"), ("💬", "autoreply"), ("🛡️", "moderation"), ("👋", "welcome"))))


def panel_markup():
    return kb([("📊 Statistics", "stats")],
              [("🤖 AI ON/OFF", "toggle:ai"), ("💬 Auto Reply", "toggle:autoreply")],
              [("🛡️ Moderation", "toggle:moderation"), ("👋 Welcome", "toggle:welcome")],
              [("📢 Broadcast Help", "broadcast_help")], [("🏠 Menu", "go:menu")])


def stats_text():
    def n(q, p=()):
        return run(q, p, "one")[0]
    now = pytime.time()
    return (
        "📊 BOT STATISTICS\n\n"
        f"👥 Users: {n('SELECT COUNT(*) FROM users')}\n"
        f"🟢 Active 24h: {n('SELECT COUNT(*) FROM users WHERE last_seen>?', (now - 86400,))}\n"
        f"📅 Active 7d: {n('SELECT COUNT(*) FROM users WHERE last_seen>?', (now - 7 * 86400,))}\n"
        f"🔥 Streak 3+: {n('SELECT COUNT(*) FROM users WHERE streak>=3')}\n"
        f"🔔 Notifications ON: {n('SELECT COUNT(*) FROM users WHERE notify=1')}\n"
        f"💬 Messages: {get_stat('messages')}\n🤖 AI replies: {get_stat('ai_replies')}\n"
        f"🎮 Games played: {get_stat('games')}\n🗑️ Deleted: {get_stat('deleted_messages')}\n"
        f"🤖 AI: {get_setting('ai')}"
    )


@admin
async def panel(u, c):
    await say(u, panel_text(), reply_markup=panel_markup())


@admin
async def stats(u, c):
    await say(u, stats_text())


@admin
async def database_info(u, c):
    size = os.path.getsize(DB_PATH) / 1024 if os.path.exists(DB_PATH) else 0
    await say(u, f"💾 DATABASE\n\n📁 {DB_PATH}\n📦 {size:.2f} KB\n🛡️ Persistent: {'YES ✅' if PERSISTENT else 'NO ⚠️'}")


@admin
async def backup(u, c):
    with open(snapshot(), "rb") as f:
        await u.effective_message.reply_document(f, filename=f"automation_{today()}.db")


@admin
async def broadcast(u, c):
    message = arg(u)
    if not message:
        return await say(u, "Usage:\n/broadcast Your message here")
    users = run("SELECT user_id FROM users", fetch="all")
    status = await say(u, "📢 Broadcast शुरू हो रहा है...")
    sent = failed = 0
    for (uid,) in users:
        try:
            await c.bot.send_message(uid, message)
            sent += 1
        except Forbidden:
            failed += 1
            run("UPDATE users SET notify=0 WHERE user_id=?", (uid,))
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"📢 Broadcast Complete\n\n✅ Sent: {sent}\n❌ Failed: {failed}")


@admin
async def addreply(u, c):
    k, _, r = arg(u).partition("|")
    k, r = k.strip().lower(), r.strip()
    if not (k and r):
        return await say(u, "Usage:\n/addreply hello | Hello! कैसे हैं?")
    run("INSERT OR REPLACE INTO auto_replies (keyword, response) VALUES (?, ?)", (k, r))
    await say(u, f"✅ Auto reply added for: {k}")


@admin
async def delreply(u, c):
    k = arg(u).lower()
    ok = k and run("DELETE FROM auto_replies WHERE keyword=?", (k,)).rowcount
    await say(u, "✅ Auto reply deleted." if ok else "❌ Usage: /delreply keyword (ya keyword nahi mila)")


@admin
async def replies(u, c):
    rows = run("SELECT keyword, response FROM auto_replies", fetch="all")
    await reply_long(u.effective_message, "📝 AUTO REPLIES\n\n" + "\n".join(f"• {k} → {r}" for k, r in rows) if rows else "No auto replies found.")


@admin
async def addban(u, c):
    w = arg(u).lower()
    if not w:
        return await say(u, "Usage: /addban word")
    run("INSERT OR IGNORE INTO banned_words VALUES (?)", (w,))
    await say(u, f"🚫 Banned word added: {w}")


@admin
async def delban(u, c):
    w = arg(u).lower()
    ok = w and run("DELETE FROM banned_words WHERE word=?", (w,)).rowcount
    await say(u, "✅ Banned word removed." if ok else "❌ Usage: /delban word (ya word nahi mila)")


def make_setting_handler(key):
    @admin
    async def handler(u, c):
        v = c.args[0].lower() if c.args else ""
        if v not in ("on", "off"):
            return await say(u, f"Usage: /{key} on/off\nCurrent: {get_setting(key)}")
        set_setting(key, v)
        await say(u, f"✅ {key} set to {v}")
    return handler


async def welcome_new_member(u, c):
    if get_setting("welcome") != "on" or not u.message:
        return
    for member in u.message.new_chat_members:
        if not member.is_bot:
            await u.message.reply_text(
                f"👋 Welcome {member.mention_html()}!\n\nहमारे group में आपका स्वागत है।", parse_mode=ParseMode.HTML)


# ---------- schedules (admin, daily group/chat message) ----------


async def scheduled_send(c):
    try:
        await c.bot.send_message(chat_id=c.job.chat_id, text=c.job.data)
    except Exception:
        logger.exception("Scheduled message failed")


def register_job(app, sid, chat_id, hh, mm, message):
    app.job_queue.run_daily(scheduled_send, time=time(hh, mm, tzinfo=TZ), chat_id=chat_id, name=f"sched_{sid}", data=message)


@admin
async def schedule_cmd(u, c):
    if c.application.job_queue is None:
        return await say(u, 'JobQueue नहीं मिला। Install करें:\npip install "python-telegram-bot[job-queue]"')
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*\|\s*(.+)$", arg(u), re.S)
    if not m:
        return await say(u, "Usage:\n/schedule 09:30 | Good morning everyone!")
    hh, mm, message = int(m[1]), int(m[2]), m[3].strip()
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return await say(u, "❌ समय गलत है। 00:00 से 23:59 के बीच दें।")
    chat_id = u.effective_chat.id
    cur = run("INSERT INTO schedules (chat_id, hh, mm, message) VALUES (?,?,?,?)", (chat_id, hh, mm, message))
    register_job(c.application, cur.lastrowid, chat_id, hh, mm, message)
    await say(u, f"⏰ Schedule #{cur.lastrowid} सेट: रोज़ {hh:02d}:{mm:02d} ({TZ.key})")


@admin
async def schedules_cmd(u, c):
    rows = run("SELECT id, hh, mm, message FROM schedules WHERE chat_id=?", (u.effective_chat.id,), "all")
    await reply_long(u.effective_message, "⏰ SCHEDULES\n\n" + "\n".join(f"#{i} — {h:02d}:{m:02d} — {t[:60]}" for i, h, m, t in rows)
                     if rows else "इस chat में कोई schedule नहीं है।")


@admin
async def delschedule_cmd(u, c):
    if not c.args or not c.args[0].isdigit():
        return await say(u, "Usage: /delschedule id")
    sid = int(c.args[0])
    if not run("DELETE FROM schedules WHERE id=? AND chat_id=?", (sid, u.effective_chat.id)).rowcount:
        return await say(u, "❌ Schedule नहीं मिला।")
    if c.application.job_queue:
        for job in c.application.job_queue.get_jobs_by_name(f"sched_{sid}"):
            job.schedule_removal()
    await say(u, "✅ Schedule deleted.")


# ==================================================
# CALLBACKS / ROUTING
# ==================================================

PUBLIC = {
    "start": start, "menu": menu, "help": help_command, "id": show_id, "clear": clear_memory,
    "daily": daily, "me": me, "top": top, "games": games, "quiz": quiz, "riddle": riddle,
    "dice": dice, "slot": slot, "darts": darts, "guess": guess_start,
    "mode": mode_cmd, "invite": invite, "notify": notify,
    "remind": remind, "reminders": reminders, "delremind": delremind,
}
ADMIN_CMDS = {
    "panel": panel, "stats": stats, "dbinfo": database_info, "backup": backup, "broadcast": broadcast,
    "addreply": addreply, "delreply": delreply, "replies": replies, "addban": addban, "delban": delban,
    "schedule": schedule_cmd, "schedules": schedules_cmd, "delschedule": delschedule_cmd,
}
MENU = {"🏠 Menu": menu, "🎮 Games": games, "🎁 Daily": daily, "👤 Profile": me, "🏆 Top": top, "🎭 Mode": mode_cmd, "🔗 Invite": invite}
GO = {"menu", "reminders", "daily", "games", "quiz", "riddle", "dice", "slot", "darts", "guess", "me", "top", "mode", "invite", "help"}


async def callbacks(u, c):
    q, d = u.callback_query, u.callback_query.data or ""
    uid = u.effective_user.id
    if d.startswith("go:") and d[3:] in GO:
        await q.answer()
        return await PUBLIC[d[3:]](u, c)
    if d.startswith("qz:"):
        _, qid, chosen = d.split(":")
        return await quiz_answer(u, c, qid, chosen)
    if d.startswith("md:") and d[3:] in MODES:
        run("UPDATE users SET mode=? WHERE user_id=?", (d[3:], uid))
        HISTORY.pop(uid, None)
        await q.answer()
        return await q.edit_message_text(f"✅ AI mode: {MODES[d[3:]][0]}\nAb kuch bhi poochho!")
    if d == "rd":
        ans = c.user_data.pop("riddle", None)
        if not ans:
            return await q.answer("Jawab pehle hi dekh liya.", show_alert=True)
        await q.answer()
        up, coins = game_reward(uid, 5, 3)
        return await say(u, f"💡 Jawab: {ans}\n🪙 +{coins}" + (UP if up else ""))
    if d == "nt":
        v = 1 - profile(uid)[4]
        run("UPDATE users SET notify=? WHERE user_id=?", (v, uid))
        return await q.answer(f"🔔 Notifications {'ON' if v else 'OFF'}", show_alert=True)
    if d in ("ai:more", "ai:short"):
        await q.answer("⏳")
        if not HISTORY.get(uid):
            return await say(u, "Pehle koi sawal poochho 🙂")
        return await ai_reply(u, c, "Isi ko aur detail me samjhao." if d == "ai:more" else "Isi ko 2 lines me short karo.")
    if not await admin_only(u):
        return
    await q.answer()
    if d.startswith("ad:") and d[3:] in ADMIN_SAFE:
        await ADMIN_CMDS[d[3:]](u, c)
    elif d == "stats":
        await say(u, stats_text())
    elif d.startswith("toggle:") and d[7:] in SETTING_DEFAULTS:
        key = d[7:]
        set_setting(key, "off" if get_setting(key) == "on" else "on")
        await q.edit_message_text(panel_text(), reply_markup=panel_markup())
    elif d == "broadcast_help":
        await say(u, "Usage:\n/broadcast Your message here")


async def error_handler(update, c):
    logger.error("Unhandled error", exc_info=c.error)


async def post_init(app):
    await app.bot.set_my_commands([BotCommand(n, t) for n, t in (
        ("start", "Bot shuru karo"), ("menu", "🏠 Main menu"), ("daily", "🎁 Daily reward"), ("games", "🎮 Games"),
        ("quiz", "🧠 Quiz"), ("me", "👤 Profile"), ("top", "🏆 Leaderboard"), ("mode", "🎭 AI style"),
        ("invite", "🔗 Invite & earn"), ("remind", "⏰ Reminder"), ("help", "📚 Help"))])
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())  # type box ke left me commands menu
    try:  # bot open karne par description ke saath START button dikhta hai
        await app.bot.set_my_description(f"👋 Welcome to {BOT_NAME}!\nShuru karne ke liye START dabao.")
        await app.bot.set_my_short_description(f"{BOT_NAME} - START dabao")
    except Exception as e:
        logger.warning(f"Description set nahi hui: {e}")
    jq = app.job_queue
    if jq is None:
        return logger.warning("JobQueue missing - schedules/reminders/notifications disabled")
    for sid, chat_id, hh, mm, message in run("SELECT id, chat_id, hh, mm, message FROM schedules", fetch="all"):
        register_job(app, sid, chat_id, hh, mm, message)
    now = pytime.time()
    for rid, due in run("SELECT id, due FROM reminders", fetch="all"):
        jq.run_once(fire_reminder, max(due - now, 5), data=rid, name=f"rem_{rid}")
    jq.run_daily(push_job, time(MORNING_HOUR, tzinfo=TZ), data="m", name="push_m")
    jq.run_daily(push_job, time(EVENING_HOUR, tzinfo=TZ), data="e", name="push_e")
    jq.run_daily(auto_backup, time(3, tzinfo=TZ), name="auto_backup")


def main():
    init_db()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    for name, fn in {**PUBLIC, **ADMIN_CMDS}.items():
        app.add_handler(CommandHandler(name, fn))
    for key in SETTING_DEFAULTS:
        app.add_handler(CommandHandler(key, make_setting_handler(key)))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)
    print(f"\n{'=' * 50}\n🤖 {BOT_NAME} Started!\n💾 DB: {DB_PATH}\n"
          f"🛡️ Persistent: {'ENABLED ✅' if PERSISTENT else 'DISABLED ⚠️'}\n{'=' * 50}\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
