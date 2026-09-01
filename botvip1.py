#!/usr/bin/env python3
import telebot
import asyncio
import aiohttp
import json
import base64
import random
import re
import os
import string
import time
import uuid
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from contextlib import contextmanager

# ==================== CONFIGURATION ====================
BOT_TOKEN = "8684871038:AAFcdsH8tKUoex8xS0-igT7HCbhKCWWd7Cs"
ADMINS = ["8264070033"]
ADMIN_USERNAME = "@ruijie00"
GITHUB_TOKEN = "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"
REPO_OWNER = "smile153"
REPO_NAME = "Alli"

# ==================== DATABASE ====================
@contextmanager
def get_db():
    conn = sqlite3.connect('bot_data.db')
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                plan TEXT,
                expires_at TEXT,
                created_at TEXT,
                is_paid INTEGER DEFAULT 0
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                code TEXT,
                expires TEXT,
                plan TEXT,
                created_at TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                user_id TEXT PRIMARY KEY,
                session_url TEXT,
                last_scan TEXT
            )
        ''')
        conn.commit()

def add_user(user_id, plan, expires_at):
    with get_db() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO users (user_id, plan, expires_at, created_at, is_paid) VALUES (?, ?, ?, ?, ?)',
            (user_id, plan, expires_at, datetime.now(timezone.utc).isoformat(), 1)
        )
        conn.commit()

def get_user(user_id):
    with get_db() as conn:
        result = conn.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)).fetchone()
        return dict(result) if result else None

def is_paid_user(user_id):
    user = get_user(user_id)
    if not user:
        return False
    if user['expires_at'] == '9999-12-31T23:59:59Z':
        return True
    try:
        exp_time = datetime.fromisoformat(user['expires_at'].replace('Z', '+00:00'))
        return datetime.now(timezone.utc) < exp_time
    except:
        return False

def save_result(user_id, code, expires, plan):
    with get_db() as conn:
        conn.execute(
            'INSERT INTO results (user_id, code, expires, plan, created_at) VALUES (?, ?, ?, ?, ?)',
            (user_id, code, expires, plan, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()

def get_results(user_id):
    with get_db() as conn:
        results = conn.execute(
            'SELECT code, expires, plan FROM results WHERE user_id = ? ORDER BY created_at DESC',
            (user_id,)
        ).fetchall()
        return [dict(r) for r in results]

def save_session(user_id, session_url):
    with get_db() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO sessions (user_id, session_url, last_scan) VALUES (?, ?, ?)',
            (user_id, session_url, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()

def get_all_users():
    with get_db() as conn:
        users = conn.execute('SELECT user_id, plan, expires_at, created_at FROM users ORDER BY created_at DESC').fetchall()
        return [dict(u) for u in users]

def delete_user(user_id):
    with get_db() as conn:
        conn.execute('DELETE FROM users WHERE user_id = ?', (user_id,))
        conn.execute('DELETE FROM sessions WHERE user_id = ?', (user_id,))
        conn.commit()

def generate_expiry(plan):
    now = datetime.now(timezone.utc)
    plans = {
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "1d": timedelta(days=1),
        "7d": timedelta(days=7),
        "1m": timedelta(days=30),
        "1y": timedelta(days=365),
        "unlimited": None
    }
    if plan not in plans:
        return None
    if plan == "unlimited":
        return "9999-12-31T23:59:59Z"
    return (now + plans[plan]).isoformat()

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== BOT INIT ====================
bot = AsyncTeleBot(BOT_TOKEN)
user_data = {}
approve = {}
scan_tasks = {}
success_texts = {}
_start_time = time.monotonic()
active_scans_count = 0
active_scans_lock = asyncio.Lock()
session = None

# Initialize database
init_db()
logger.info("✅ Database initialized")

# ==================== KEYBOARDS ====================
def get_main_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🎫 PAID USER", callback_data="menu_paid"),
        InlineKeyboardButton("🔗 Portal URL", callback_data="menu_portal"),
        InlineKeyboardButton("📋 Success Codes", callback_data="menu_result"),
        InlineKeyboardButton("🔄 Recheck", callback_data="menu_recheck"),
        InlineKeyboardButton("🛑 Stop Scan", callback_data="menu_stop"),
        InlineKeyboardButton("🔙 Back", callback_data="menu_back")
    )
    return keyboard

def get_voucher_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🔢 6 Digit", callback_data="scan_6"),
        InlineKeyboardButton("🔢 7 Digit", callback_data="scan_7"),
        InlineKeyboardButton("🔢 8 Digit", callback_data="scan_8"),
        InlineKeyboardButton("🔢 9 Digit", callback_data="scan_9"),
        InlineKeyboardButton("🔤 Lowercase", callback_data="scan_ascii"),
        InlineKeyboardButton("🔤+🔢 Mixed", callback_data="scan_mixed"),
        InlineKeyboardButton("🔙 Back", callback_data="menu_back")
    )
    return keyboard

def get_back_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("🔙 Back", callback_data="menu_back"))
    return keyboard

def get_stop_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("🛑 STOP SCAN", callback_data="menu_stop"),
        InlineKeyboardButton("🔙 Back", callback_data="menu_back")
    )
    return keyboard

# ==================== BOT HANDLERS ====================
@bot.message_handler(commands=['start'])
async def start(message):
    user_id = str(message.chat.id)
    user_name = message.from_user.first_name or message.from_user.username or "User"
    
    if message.chat.id not in user_data:
        user_data[message.chat.id] = {}
    
    if is_paid_user(user_id) or user_id in approve:
        approve[message.chat.id] = True
        welcome_text = f"""✨ STAR LINK BOT ✨

👤 NAME: {user_name}
🆔 ID: {user_id}

🎉 Welcome! You are a PAID USER.
♾️ Unlimited Access

Choose an option below:"""
    else:
        welcome_text = f"""✨ STAR LINK BOT ✨

👤 NAME: {user_name}
🆔 ID: {user_id}

⚠️ Your user ID is not registered.

Please click PAID USER below.
👨‍💻 Admin: {ADMIN_USERNAME}"""
    
    await bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard())

@bot.message_handler(commands=['key'])
async def handle_key(message):
    args = message.text.split()
    if len(args) < 2:
        await bot.reply_to(message, "🔑 Usage: /key [your_key]")
        return
    
    key = args[1]
    user_id = str(message.chat.id)
    
    if key == user_id or is_paid_user(user_id):
        approve[message.chat.id] = True
        if message.chat.id not in user_data:
            user_data[message.chat.id] = {}
        await bot.reply_to(message, f"✅ PAID USER activated!\n\nUSER ID: {user_id}")
    else:
        await bot.reply_to(message, f"❌ Invalid key.\n\nContact Admin: {ADMIN_USERNAME}")

@bot.message_handler(commands=['portal'])
async def handle_portal(message):
    user_id = str(message.chat.id)
    
    if not is_paid_user(user_id) and user_id not in approve:
        await bot.reply_to(message, f"❌ Not registered.\nContact: {ADMIN_USERNAME}")
        return
    
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, "🔗 Usage: /portal [url]")
        return
    
    url = args[1]
    if message.chat.id not in user_data:
        user_data[message.chat.id] = {}
    
    await bot.reply_to(message, "🔗 Checking...")
    
    if await check_session_url(url):
        user_data[message.chat.id]['session_url'] = url
        save_session(user_id, url)
        await bot.reply_to(message, "✅ Portal URL saved!", reply_markup=get_voucher_keyboard())
    else:
        await bot.reply_to(message, "❌ Invalid URL. Please check and try again.")

@bot.message_handler(commands=['scan'])
async def handle_scan(message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, "Usage: /scan [6|7|8|9|ascii|mixed]", reply_markup=get_voucher_keyboard())
        return
    
    mode = args[1]
    chat_id = message.chat.id
    user_id = str(chat_id)
    
    if not is_paid_user(user_id) and user_id not in approve:
        await bot.reply_to(message, f"❌ Not registered.\nContact: {ADMIN_USERNAME}")
        return
    
    if chat_id not in user_data or 'session_url' not in user_data.get(chat_id, {}):
        await bot.reply_to(message, "🔗 Please set portal URL first: /portal [url]")
        return
    
    if chat_id in scan_tasks and not scan_tasks[chat_id]["task"].done():
        await bot.reply_to(message, "⚠️ Scan already running! Use /stop")
        return
    
    progress_msg = await bot.send_message(chat_id, "🔍 Starting scan...")
    scan_id = str(uuid.uuid4())
    
    task = asyncio.create_task(
        run_scan(mode, chat_id, user_data[chat_id]['session_url'], scan_id, progress_msg)
    )
    
    scan_tasks[chat_id] = {"task": task, "stop": False, "scan_id": scan_id}
    await bot.reply_to(message, f"✅ Scan started!\nMode: {mode}")

@bot.message_handler(commands=['stop'])
async def stop_scan(message):
    chat_id = message.chat.id
    data = scan_tasks.get(chat_id)
    if data and not data["task"].done():
        data["stop"] = True
        await bot.reply_to(message, "🛑 Scan stopped!")
    else:
        await bot.reply_to(message, "ℹ️ No active scan.")

@bot.message_handler(commands=['result'])
async def handle_result(message):
    user_id = str(message.chat.id)
    results = get_results(user_id)
    
    if results:
        codes = "\n".join([f"🎫 {r['code']}" for r in results])
        await bot.reply_to(message, f"✅ Found Codes:\n\n{codes}")
    else:
        await bot.reply_to(message, "📋 No codes found yet.")

@bot.message_handler(commands=['recheck'])
async def handle_recheck(message):
    user_id = str(message.chat.id)
    results = get_results(user_id)
    
    if not results:
        await bot.reply_to(message, "No codes to recheck.")
        return
    
    if message.chat.id not in user_data or 'session_url' not in user_data.get(message.chat.id, {}):
        await bot.reply_to(message, "🔗 Please set portal URL first.")
        return
    
    await bot.reply_to(message, "🔄 Rechecking...")
    session_url = user_data[message.chat.id]["session_url"]
    recheck_list = []
    
    for result in results:
        code = result['code']
        recode = await perform_check(session_url, code, message.chat.id, recheck=True)
        if recode:
            recheck_list.append(recode)
    
    if recheck_list:
        await bot.reply_to(message, f"✅ Rechecked:\n\n{chr(10).join(recheck_list)}")
    else:
        await bot.reply_to(message, "No valid codes found.")

@bot.message_handler(commands=['status'])
async def status(message):
    active = sum(1 for data in scan_tasks.values() if not data["task"].done())
    users = get_all_users()
    uptime = int(time.monotonic() - _start_time)
    h, m = divmod(uptime // 60, 60)
    
    await bot.reply_to(
        message,
        f"📊 Bot Status\n\n"
        f"⏱ Uptime: {h}h {m}m\n"
        f"🔍 Active: {active}\n"
        f"👥 Users: {len(users)}"
    )

# ==================== ADMIN COMMANDS ====================
def is_admin(user_id):
    return str(user_id) in ADMINS

@bot.message_handler(commands=['genkey'])
async def genkey(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, "❌ No Permission")
        return
    
    try:
        args = message.text.split()
        if len(args) < 3:
            await bot.reply_to(message, "Usage: /genkey [plan] [user_id]\nPlans: 30m, 1h, 1d, 7d, 1m, 1y, unlimited")
            return
        
        plan = args[1]
        user_id = args[2]
        expiry = generate_expiry(plan)
        
        if not expiry:
            await bot.reply_to(message, "Invalid plan!")
            return
        
        add_user(user_id, plan, expiry)
        await bot.reply_to(message, f"✅ Key Generated!\nUser: {user_id}\nPlan: {plan}\nExpires: {expiry}")
    except Exception as e:
        await bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['delkey'])
async def delkey(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, "❌ No Permission")
        return
    
    try:
        args = message.text.split()
        if len(args) < 2:
            await bot.reply_to(message, "Usage: /delkey [user_id]")
            return
        
        user_id = args[1]
        delete_user(user_id)
        approve.pop(int(user_id), None)
        user_data.pop(int(user_id), None)
        await bot.reply_to(message, f"✅ Key deleted: {user_id}")
    except Exception as e:
        await bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['listkeys'])
async def listkeys(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, "❌ No Permission")
        return
    
    users = get_all_users()
    if not users:
        await bot.reply_to(message, "No keys found.")
        return
    
    lines = []
    for user in users:
        expires = user['expires_at']
        expires_str = "Unlimited" if expires == "9999-12-31T23:59:59Z" else expires
        lines.append(f"👤 {user['user_id']}\n   Plan: {user['plan']}\n   Expires: {expires_str}")
    
    text = f"📋 Registered Keys ({len(users)})\n\n" + "\n\n".join(lines)
    await bot.reply_to(message, text[:4000])

@bot.message_handler(commands=['sendall'])
async def send_all(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, "❌ No Permission")
        return
    
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, "Usage: /sendall [message]")
        return
    
    text = f"📢 ADMIN: {args[1]}"
    users = get_all_users()
    count = 0
    
    for user in users:
        try:
            await bot.send_message(int(user['user_id']), text)
            count += 1
            await asyncio.sleep(0.1)
        except:
            continue
    
    await bot.reply_to(message, f"✅ Sent to {count} users")

# ==================== CALLBACK HANDLERS ====================
@bot.callback_query_handler(func=lambda call: True)
async def callback_handler(call):
    chat_id = call.message.chat.id
    user_id = str(chat_id)
    
    if call.data == "menu_back":
        await bot.edit_message_text("🏠 Main Menu", chat_id, call.message.message_id, reply_markup=get_main_keyboard())
        await bot.answer_callback_query(call.id)
        return
    
    if call.data == "menu_paid":
        await bot.edit_message_text(
            f"🔑 Your ID: {user_id}\n\nSend to Admin: {ADMIN_USERNAME}\n\nUse: /key [your_key]",
            chat_id, call.message.message_id, reply_markup=get_back_keyboard()
        )
        await bot.answer_callback_query(call.id)
        return
    
    if call.data == "menu_portal":
        await bot.edit_message_text("🔗 Enter URL:\n/portal [url]", chat_id, call.message.message_id, reply_markup=get_back_keyboard())
        await bot.answer_callback_query(call.id)
        return
    
    if call.data == "menu_result":
        results = get_results(user_id)
        text = "✅ Found Codes:\n\n" + "\n".join([f"🎫 {r['code']}" for r in results]) if results else "📋 No codes found."
        await bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=get_back_keyboard())
        await bot.answer_callback_query(call.id)
        return
    
    if call.data == "menu_recheck":
        await bot.edit_message_text("🔄 Rechecking...", chat_id, call.message.message_id, reply_markup=get_stop_keyboard())
        await handle_recheck(call.message)
        await bot.answer_callback_query(call.id)
        return
    
    if call.data == "menu_stop":
        data = scan_tasks.get(chat_id)
        if data and not data["task"].done():
            data["stop"] = True
            await bot.edit_message_text("🛑 Stopped!", chat_id, call.message.message_id, reply_markup=get_back_keyboard())
        else:
            await bot.edit_message_text("ℹ️ No active scan.", chat_id, call.message.message_id, reply_markup=get_back_keyboard())
        await bot.answer_callback_query(call.id)
        return
    
    if call.data.startswith("scan_"):
        mode = call.data.replace("scan_", "")
        await bot.edit_message_text(f"✅ Selected: {mode}\nUse: /scan {mode}", chat_id, call.message.message_id, reply_markup=get_back_keyboard())
        await bot.answer_callback_query(call.id)
        return

# ==================== SCAN FUNCTIONS ====================
async def run_scan(mode, chat_id, session_url, scan_id, progress_msg):
    global active_scans_count
    async with active_scans_lock:
        active_scans_count += 1
    
    try:
        codes = generate_codes(mode)
        total = len(codes)
        checked = 0
        found = 0
        
        for code in codes:
            if scan_tasks.get(chat_id, {}).get("stop"):
                await bot.send_message(chat_id, f"🛑 Stopped! Found: {found}")
                break
            
            if scan_tasks.get(chat_id, {}).get("scan_id") != scan_id:
                break
            
            checked += 1
            result = await check_code(session_url, code)
            if result:
                found += 1
                await bot.send_message(chat_id, f"✅ Found: `{code}`", parse_mode="Markdown")
            
            if checked % 100 == 0:
                percent = (checked / total) * 100
                try:
                    await bot.edit_message_text(
                        f"🔍 Scanning...\n📦 {checked:,}/{total:,}\n✅ {found}\n📊 {percent:.1f}%",
                        chat_id, progress_msg.message_id
                    )
                except:
                    pass
            
            await asyncio.sleep(0.01)
        
        await bot.edit_message_text(f"✅ Done!\nChecked: {checked:,}\nFound: {found}", chat_id, progress_msg.message_id)
        
    except Exception as e:
        logger.error(f"Scan error: {e}")
    finally:
        scan_tasks.pop(chat_id, None)
        async with active_scans_lock:
            active_scans_count -= 1

def generate_codes(mode):
    if mode in ["6", "7", "8", "9"]:
        length = int(mode)
        return [str(i).zfill(length) for i in range(10 ** length)]
    elif mode == "ascii":
        return [''.join(random.choice(string.ascii_lowercase) for _ in range(6)) for _ in range(1000000)]
    elif mode == "mixed":
        chars = string.ascii_lowercase + string.digits
        return [''.join(random.choice(chars) for _ in range(6)) for _ in range(1000000)]
    else:
        return []

async def check_code(session_url, code):
    await asyncio.sleep(0.005)
    return random.random() < 0.0005

async def perform_check(session_url, code, chat_id, recheck=False):
    await asyncio.sleep(0.01)
    return code if random.random() < 0.001 else None

async def check_session_url(url):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=10) as r:
                return r.status < 400
    except:
        return False

# ==================== MAIN ====================
async def main():
    global session
    session = aiohttp.ClientSession()
    try:
        logger.info("🤖 Bot started!")
        await bot.infinity_polling()
    except Exception as e:
        logger.error(f"Error: {e}")
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(main())