# -*- coding: utf-8 -*-
"""
🤖 SHADOW HOSTING BOT - Fixed Working Version
Host Python (.py) / JS (.js) scripts + ZIP archives
"""
import os, sys, re, time, sqlite3, subprocess, tempfile, shutil, zipfile
import logging, atexit, threading
from datetime import datetime, timedelta
from threading import Thread
import telebot
from telebot import types
import psutil
from flask import Flask

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ───────────────────────── CONFIG ─────────────────────────
TOKEN = os.getenv('TOKEN', '8620671084:AAHi_Za_rDVYdPwhzH66pT-kj8E5wXD53gI')
OWNER_ID = int(os.getenv('OWNER_ID', '7986731915'))
ADMIN_ID = int(os.getenv('ADMIN_ID', '7986731915'))
YOUR_USERNAME = os.getenv('YOUR_USERNAME', 'King_X_vau')
UPDATE_CHANNEL = os.getenv('UPDATE_CHANNEL', 'King_X_vau')

FREE_USER_LIMIT = int(os.getenv('FREE_USER_LIMIT', 2))
SUBSCRIBED_USER_LIMIT = int(os.getenv('SUBSCRIBED_USER_LIMIT', 20000))
ADMIN_LIMIT = int(os.getenv('ADMIN_LIMIT', 999))
OWNER_LIMIT = float('inf')

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_BOTS_DIR = os.path.join(BASE_DIR, 'upload_bots')
IROTECH_DIR = os.path.join(BASE_DIR, 'inf')
DATABASE_PATH = os.path.join(IROTECH_DIR, 'bot_data.db')
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB

os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True)
os.makedirs(IROTECH_DIR, exist_ok=True)

if not TOKEN:
    raise SystemExit("❌ TOKEN missing!")

bot = telebot.TeleBot(TOKEN)
DB_LOCK = threading.Lock()

# ───────────────────────── STATE ─────────────────────────
bot_scripts = {}          # key: "{owner}_{file}" -> {process, log_file, ...}
user_subscriptions = {}
user_files = {}           # uid -> [(file_name, type)]
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
banned_users = set()
user_limits = {}
bot_locked = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ───────────────────────── FLASK KEEP-ALIVE ─────────────────────────
flask_app = Flask('__main__')

@flask_app.route('/')
def home():
    return "🤖 SHADOW HOSTING BOT is alive"

def keep_alive():
    try:
        port = int(os.environ.get("PORT", 8080))
        t = Thread(target=lambda: flask_app.run(
            host='0.0.0.0', port=port, use_reloader=False
        ))
        t.daemon = True
        t.start()
        logger.info(f"✅ Flask keep-alive on port {port}")
    except Exception as e:
        logger.error(f"Keep-alive failed: {e}")

# ───────────────────────── DATABASE ─────────────────────────
def init_db():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS subscriptions (user_id INTEGER PRIMARY KEY, expiry TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS user_files (user_id INTEGER, file_name TEXT, file_type TEXT, PRIMARY KEY (user_id, file_name))')
    c.execute('CREATE TABLE IF NOT EXISTS active_users (user_id INTEGER PRIMARY KEY, join_date TEXT, last_seen TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, added_by INTEGER, added_date TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS banned_users (user_id INTEGER PRIMARY KEY, reason TEXT, banned_by INTEGER, ban_date TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS user_limits (user_id INTEGER PRIMARY KEY, file_limit INTEGER, set_by INTEGER, set_date TEXT)')
    c.execute('INSERT OR IGNORE INTO admins (user_id, added_by, added_date) VALUES (?, ?, ?)',
              (OWNER_ID, OWNER_ID, datetime.now().isoformat()))
    if ADMIN_ID != OWNER_ID:
        c.execute('INSERT OR IGNORE INTO admins (user_id, added_by, added_date) VALUES (?, ?, ?)',
                  (ADMIN_ID, OWNER_ID, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def load_data():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    for uid, exp in c.execute('SELECT user_id, expiry FROM subscriptions'):
        try:
            user_subscriptions[uid] = {'expiry': datetime.fromisoformat(exp)}
        except Exception:
            pass
    for uid, fn, ft in c.execute('SELECT user_id, file_name, file_type FROM user_files'):
        user_files.setdefault(uid, []).append((fn, ft))
    active_users.update(u for (u,) in c.execute('SELECT user_id FROM active_users'))
    admin_ids.update(u for (u,) in c.execute('SELECT user_id FROM admins'))
    banned_users.update(u for (u,) in c.execute('SELECT user_id FROM banned_users'))
    for uid, fl in c.execute('SELECT user_id, file_limit FROM user_limits'):
        user_limits[uid] = fl
    conn.close()

init_db()
load_data()

# ───────────────────────── HELPERS ─────────────────────────
def get_user_folder(uid):
    folder = os.path.join(UPLOAD_BOTS_DIR, str(uid))
    os.makedirs(folder, exist_ok=True)
    return folder

def get_user_file_limit(uid):
    if uid == OWNER_ID: return OWNER_LIMIT
    if uid in admin_ids: return ADMIN_LIMIT
    if uid in user_limits: return user_limits[uid]
    if uid in user_subscriptions and user_subscriptions[uid]['expiry'] > datetime.now():
        return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT

def get_user_file_count(uid):
    return len(user_files.get(uid, []))

def is_running(owner_id, file_name):
    key = f"{owner_id}_{file_name}"
    info = bot_scripts.get(key)
    if not info or not info.get('process'):
        return False
    try:
        p = psutil.Process(info['process'].pid)
        if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
            return True
    except psutil.NoSuchProcess:
        pass
    try:
        if info.get('log_file') and not info['log_file'].closed:
            info['log_file'].close()
    except Exception:
        pass
    bot_scripts.pop(key, None)
    return False

def kill_tree(info):
    """Kill a process and all its children + close log file."""
    try:
        lf = info.get('log_file')
        if lf and not lf.closed:
            try: lf.close()
            except Exception: pass
    except Exception:
        pass
    proc = info.get('process')
    if not proc or not getattr(proc, 'pid', None):
        return
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for ch in children:
            try: ch.terminate()
            except Exception: pass
        try: parent.terminate()
        except Exception: pass
        gone, alive = psutil.wait_procs([parent] + children, timeout=2)
        for a in alive:
            try: a.kill()
            except Exception: pass
    except psutil.NoSuchProcess:
        pass
    except Exception as e:
        logger.error(f"kill_tree error: {e}")

# ───────────────────────── AUTO-INSTALL ─────────────────────────
MODULE_MAP = {
    'telebot': 'pyTelegramBotAPI',
    'telegram': 'python-telegram-bot',
    'bs4': 'beautifulsoup4',
    'pillow': 'Pillow',
    'pil': 'Pillow',
    'cv2': 'opencv-python',
    'yaml': 'PyYAML',
    'dotenv': 'python-dotenv',
    'dateutil': 'python-dateutil',
}
CORE_MODULES = {
    'os', 'sys', 're', 'time', 'math', 'random', 'logging', 'threading',
    'subprocess', 'zipfile', 'tempfile', 'shutil', 'sqlite3', 'asyncio',
    'json', 'datetime', 'atexit', 'collections', 'itertools', 'functools',
    'pathlib', 'typing', 'io', 'signal', 'struct', 'hashlib', 'base64',
}

def try_pip_install(module_name, message):
    pkg = MODULE_MAP.get(module_name.lower(), module_name)
    if module_name.lower() in CORE_MODULES:
        return False, "core module"
    try:
        bot.reply_to(message, f"🐍 Installing `{pkg}`...", parse_mode='Markdown')
        r = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', pkg],
            capture_output=True, text=True, timeout=300
        )
        if r.returncode == 0:
            bot.reply_to(message, f"✅ Installed `{pkg}`", parse_mode='Markdown')
            return True, ""
        bot.reply_to(message, f"❌ pip failed:\n```\n{(r.stderr or r.stdout)[:500]}\n```", parse_mode='Markdown')
        return False, r.stderr
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        logger.error(f"pip install error: {e}")
        return False, str(e)

def try_npm_install(module_name, folder, message):
    try:
        bot.reply_to(message, f"🟠 Installing Node module `{module_name}`...", parse_mode='Markdown')
        r = subprocess.run(
            ['npm', 'install', module_name],
            cwd=folder, capture_output=True, text=True, timeout=300
        )
        if r.returncode == 0:
            bot.reply_to(message, f"✅ Installed `{module_name}`", parse_mode='Markdown')
            return True, ""
        bot.reply_to(message, f"❌ npm failed: {(r.stderr or r.stdout)[:400]}")
        return False, r.stderr
    except FileNotFoundError:
        bot.reply_to(message, "❌ `npm` not found. Install Node.js.")
        return False, "npm not found"
    except Exception as e:
        return False, str(e)

# ───────────────────────── SCRIPT RUNNERS ─────────────────────────
def run_script(script_path, owner_id, user_folder, file_name, message, attempt=1):
    """Run Python script with auto-install of missing modules."""
    if attempt > 2:
        bot.reply_to(message, f"❌ Could not run '{file_name}' after 2 attempts. Check logs.")
        return

    key = f"{owner_id}_{file_name}"
    if not os.path.exists(script_path):
        bot.reply_to(message, f"❌ Script '{file_name}' missing!")
        return

    # Pre-check only on first attempt
    if attempt == 1:
        proc = None
        try:
            proc = subprocess.Popen(
                [sys.executable, script_path], cwd=user_folder,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            _, err = proc.communicate(timeout=5)
            if proc.returncode != 0 and err:
                m = re.search(r"ModuleNotFoundError: No module named '([^']+)'", err)
                if m:
                    mod = m.group(1).strip()
                    if mod.split('.')[0].lower() not in CORE_MODULES:
                        ok, _ = try_pip_install(mod, message)
                        if ok:
                            time.sleep(1)
                            threading.Thread(
                                target=run_script,
                                args=(script_path, owner_id, user_folder, file_name, message, attempt + 1)
                            ).start()
                            return
                    bot.reply_to(message, f"❌ Missing module `{mod}` could not be installed.")
                    return
                bot.reply_to(message, f"❌ Script error:\n```\n{err[:500]}\n```", parse_mode='Markdown')
                return
        except subprocess.TimeoutExpired:
            # Good — it's running (beyond 5s). Kill pre-check and launch real run.
            try:
                if proc and proc.poll() is None:
                    proc.kill()
                    proc.communicate()
            except Exception:
                pass
        except FileNotFoundError:
            bot.reply_to(message, f"❌ Python interpreter not found")
            return
        except Exception as e:
            logger.error(f"Pre-check error: {e}")
            # Continue — try the real run anyway

    # Actual long-run
    log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
    try:
        log_file = open(log_path, 'w', encoding='utf-8', errors='ignore')
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        process = subprocess.Popen(
            [sys.executable, script_path], cwd=user_folder,
            stdout=log_file, stderr=log_file, stdin=subprocess.PIPE,
            startupinfo=startupinfo
        )
        bot_scripts[key] = {
            'process': process, 'log_file': log_file, 'file_name': file_name,
            'script_owner_id': owner_id, 'user_folder': user_folder,
            'type': 'py', 'start_time': datetime.now(),
            'chat_id': message.chat.id, 'script_key': key,
        }
        bot.reply_to(message, f"✅ Started `{file_name}` (PID: `{process.pid}`)", parse_mode='Markdown')
        logger.info(f"Started {key} PID={process.pid}")
    except Exception as e:
        logger.error(f"Run error for {key}: {e}", exc_info=True)
        bot.reply_to(message, f"❌ Failed to start: {e}")

def run_js_script(script_path, owner_id, user_folder, file_name, message, attempt=1):
    if attempt > 2:
        bot.reply_to(message, f"❌ Could not run '{file_name}' after 2 attempts.")
        return
    key = f"{owner_id}_{file_name}"
    if not os.path.exists(script_path):
        bot.reply_to(message, f"❌ Script '{file_name}' missing!")
        return

    if attempt == 1:
        proc = None
        try:
            proc = subprocess.Popen(
                ['node', script_path], cwd=user_folder,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            _, err = proc.communicate(timeout=5)
            if proc.returncode != 0 and err:
                m = re.search(r"Cannot find module '([^']+)'", err)
                if m:
                    mod = m.group(1).strip()
                    if not mod.startswith('.') and not mod.startswith('/'):
                        ok, _ = try_npm_install(mod, user_folder, message)
                        if ok:
                            time.sleep(1)
                            threading.Thread(
                                target=run_js_script,
                                args=(script_path, owner_id, user_folder, file_name, message, attempt + 1)
                            ).start()
                            return
                    bot.reply_to(message, f"❌ Cannot install module `{mod}`")
                    return
                bot.reply_to(message, f"❌ Script error:\n```\n{err[:500]}\n```", parse_mode='Markdown')
                return
        except subprocess.TimeoutExpired:
            try:
                if proc and proc.poll() is None:
                    proc.kill()
                    proc.communicate()
            except Exception:
                pass
        except FileNotFoundError:
            bot.reply_to(message, "❌ `node` not found. Install Node.js.")
            return
        except Exception as e:
            logger.error(f"JS pre-check error: {e}")

    log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
    try:
        log_file = open(log_path, 'w', encoding='utf-8', errors='ignore')
        process = subprocess.Popen(
            ['node', script_path], cwd=user_folder,
            stdout=log_file, stderr=log_file, stdin=subprocess.PIPE
        )
        bot_scripts[key] = {
            'process': process, 'log_file': log_file, 'file_name': file_name,
            'script_owner_id': owner_id, 'user_folder': user_folder,
            'type': 'js', 'start_time': datetime.now(),
            'chat_id': message.chat.id, 'script_key': key,
        }
        bot.reply_to(message, f"✅ Started `{file_name}` (PID: `{process.pid}`)", parse_mode='Markdown')
        logger.info(f"Started JS {key} PID={process.pid}")
    except FileNotFoundError:
        bot.reply_to(message, "❌ `node` not found")
    except Exception as e:
        logger.error(f"JS run error for {key}: {e}", exc_info=True)
        bot.reply_to(message, f"❌ Failed to start: {e}")

# ───────────────────────── DB WRITES ─────────────────────────
def save_user_file(uid, fn, ft):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        try:
            c = conn.cursor()
            c.execute('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type) VALUES (?, ?, ?)',
                      (uid, fn, ft))
            conn.commit()
        finally:
            conn.close()
    if uid not in user_files:
        user_files[uid] = []
    user_files[uid] = [f for f in user_files[uid] if f[0] != fn]
    user_files[uid].append((fn, ft))

def remove_user_file(uid, fn):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        try:
            c = conn.cursor()
            c.execute('DELETE FROM user_files WHERE user_id=? AND file_name=?', (uid, fn))
            conn.commit()
        finally:
            conn.close()
    if uid in user_files:
        user_files[uid] = [f for f in user_files[uid] if f[0] != fn]

def save_subscription(uid, expiry):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        try:
            c = conn.cursor()
            c.execute('INSERT OR REPLACE INTO subscriptions (user_id, expiry) VALUES (?, ?)',
                      (uid, expiry.isoformat()))
            conn.commit()
        finally:
            conn.close()
    user_subscriptions[uid] = {'expiry': expiry}

def remove_subscription(uid):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        try:
            c = conn.cursor()
            c.execute('DELETE FROM subscriptions WHERE user_id=?', (uid,))
            conn.commit()
        finally:
            conn.close()
    user_subscriptions.pop(uid, None)

def add_active_user(uid):
    if uid in active_users:
        return
    active_users.add(uid)
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        try:
            c = conn.cursor()
            now = datetime.now().isoformat()
            c.execute('INSERT OR REPLACE INTO active_users (user_id, join_date, last_seen) VALUES (?, ?, ?)',
                      (uid, now, now))
            conn.commit()
        finally:
            conn.close()

def ban_user(uid, reason, by):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        try:
            c = conn.cursor()
            c.execute('INSERT OR REPLACE INTO banned_users (user_id, reason, banned_by, ban_date) VALUES (?, ?, ?, ?)',
                      (uid, reason, by, datetime.now().isoformat()))
            conn.commit()
        finally:
            conn.close()
    banned_users.add(uid)

def unban_user(uid):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        try:
            c = conn.cursor()
            c.execute('DELETE FROM banned_users WHERE user_id=?', (uid,))
            conn.commit()
        finally:
            conn.close()
    banned_users.discard(uid)

def set_user_limit(uid, limit, by):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        try:
            c = conn.cursor()
            c.execute('INSERT OR REPLACE INTO user_limits (user_id, file_limit, set_by, set_date) VALUES (?, ?, ?, ?)',
                      (uid, limit, by, datetime.now().isoformat()))
            conn.commit()
        finally:
            conn.close()
    user_limits[uid] = limit

# ───────────────────────── UI BUILDERS ─────────────────────────
def main_menu_inline(uid):
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(types.InlineKeyboardButton('📢 Updates Channel',
                                     url=f'https://t.me/{UPDATE_CHANNEL.replace("@", "")}'))
    m.row(
        types.InlineKeyboardButton('📤 Upload File', callback_data='upload'),
        types.InlineKeyboardButton('📂 Check Files', callback_data='check_files')
    )
    m.row(
        types.InlineKeyboardButton('⚡ Bot Speed', callback_data='speed'),
        types.InlineKeyboardButton('📊 Statistics', callback_data='stats')
    )
    m.add(types.InlineKeyboardButton('📞 Contact Owner',
                                     url=f'https://t.me/{YOUR_USERNAME.replace("@", "")}'))
    if uid in admin_ids:
        m.row(
            types.InlineKeyboardButton('💳 Subscriptions', callback_data='sub_menu'),
            types.InlineKeyboardButton('📢 Broadcast', callback_data='broadcast')
        )
        m.row(
            types.InlineKeyboardButton('🔒 Lock' if not bot_locked else '🔓 Unlock',
                                       callback_data='lock_toggle'),
            types.InlineKeyboardButton('👑 Admin Panel', callback_data='admin_panel')
        )
        m.row(
            types.InlineKeyboardButton('👥 User Management', callback_data='user_mgmt'),
            types.InlineKeyboardButton('🟢 Run All', callback_data='run_all')
        )
    return m

def status_of(uid):
    if uid == OWNER_ID: return "👑 Owner"
    if uid in admin_ids: return "🛡️ Admin"
    if uid in banned_users: return "🚫 Banned"
    if uid in user_subscriptions and user_subscriptions[uid]['expiry'] > datetime.now():
        return "⭐ Premium"
    return "🆓 Free User"

def send_welcome(message):
    uid = message.from_user.id
    chat_id = message.chat.id
    if uid in banned_users:
        bot.send_message(chat_id, "❌ You are banned from using this bot.")
        return
    if bot_locked and uid not in admin_ids:
        bot.send_message(chat_id, "⚠️ Bot is locked by admin. Try again later.")
        return
    add_active_user(uid)
    limit = get_user_file_limit(uid)
    limit_str = str(limit) if limit != float('inf') else "Unlimited"
    text = (f"〽️ Welcome, {message.from_user.first_name}!\n\n"
            f"🆔 Your ID: `{uid}`\n"
            f"🔰 Status: {status_of(uid)}\n"
            f"📁 Files: {get_user_file_count(uid)}/{limit_str}\n\n"
            f"🤖 Host Python (.py) or JS (.js) scripts\n"
            f"📦 Upload single files or `.zip` archives\n"
            f"🐍 Missing modules install automatically\n\n"
            f"👇 Use the buttons below")
    bot.send_message(chat_id, text, reply_markup=main_menu_inline(uid), parse_mode='Markdown')

# ───────────────────────── COMMANDS ─────────────────────────
@bot.message_handler(commands=['start', 'help'])
def cmd_start(m):
    send_welcome(m)

@bot.message_handler(commands=['status', 'statistics'])
def cmd_stats(m):
    if m.from_user.id in banned_users:
        return
    running = sum(1 for k, v in list(bot_scripts.items())
                  if is_running(v['script_owner_id'], v['file_name']))
    text = (f"📊 Statistics:\n\n"
            f"👥 Total users: {len(active_users)}\n"
            f"🚫 Banned: {len(banned_users)}\n"
            f"📂 Total files: {sum(len(f) for f in user_files.values())}\n"
            f"🟢 Running scripts: {running}\n"
            f"🔒 Bot: {'Locked' if bot_locked else 'Unlocked'}")
    bot.reply_to(m, text)

@bot.message_handler(commands=['ping'])
def cmd_ping(m):
    start = time.time()
    msg = bot.reply_to(m, "Pong!")
    latency = (time.time() - start) * 1000
    bot.edit_message_text(f"Pong! {latency:.0f}ms", m.chat.id, msg.message_id)

@bot.message_handler(commands=['broadcast'])
def cmd_broadcast(m):
    if m.from_user.id not in admin_ids:
        bot.reply_to(m, "⚠️ Admin only.")
        return
    msg = bot.reply_to(m, "📢 Send the message to broadcast (or /cancel).")
    bot.register_next_step_handler(msg, do_broadcast)

def do_broadcast(m):
    if m.from_user.id not in admin_ids:
        return
    if m.text and m.text.lower() == '/cancel':
        bot.reply_to(m, "❌ Cancelled.")
        return
    sent, failed = 0, 0
    bot.reply_to(m, f"📢 Broadcasting to {len(active_users)} users...")
    for uid in list(active_users):
        try:
            bot.copy_message(uid, m.chat.id, m.message_id)
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.05)
    bot.reply_to(m, f"✅ Done. Sent: {sent}, Failed: {failed}")

# ───────────────────────── FILE UPLOAD ─────────────────────────
@bot.message_handler(content_types=['document'])
def handle_doc(m):
    uid = m.from_user.id
    if uid in banned_users:
        bot.reply_to(m, "❌ You are banned.")
        return
    if bot_locked and uid not in admin_ids:
        bot.reply_to(m, "⚠️ Bot locked.")
        return

    limit = get_user_file_limit(uid)
    count = get_user_file_count(uid)
    if count >= limit:
        bot.reply_to(m, f"⚠️ File limit reached ({count}/{limit}).")
        return

    doc = m.document
    fn = doc.file_name
    if not fn:
        bot.reply_to(m, "❌ File has no name.")
        return
    ext = os.path.splitext(fn)[1].lower()
    if ext not in ('.py', '.js', '.zip'):
        bot.reply_to(m, "⚠️ Only `.py`, `.js`, or `.zip` files allowed.")
        return
    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        bot.reply_to(m, f"⚠️ File too large (max {MAX_FILE_SIZE // (1024 * 1024)}MB).")
        return

    try:
        wait = bot.reply_to(m, f"⏳ Downloading `{fn}`...", parse_mode='Markdown')
        try:
            file_info = bot.get_file(doc.file_id)
            if not file_info.file_path:
                bot.edit_message_text("❌ Cannot fetch file from Telegram.", m.chat.id, wait.message_id)
                return
            content = bot.download_file(file_info.file_path)
        except Exception as e:
            bot.edit_message_text(f"❌ Download failed: {e}", m.chat.id, wait.message_id)
            return

        folder = get_user_folder(uid)

        # ───── ZIP branch ─────
        if ext == '.zip':
            temp = tempfile.mkdtemp(prefix=f"zip_{uid}_")
            try:
                zp = os.path.join(temp, fn)
                with open(zp, 'wb') as f:
                    f.write(content)

                with zipfile.ZipFile(zp, 'r') as z:
                    base = os.path.abspath(temp)
                    for member in z.infolist():
                        target = os.path.abspath(os.path.join(temp, member.filename))
                        if not target.startswith(base):
                            bot.edit_message_text("❌ Unsafe path in ZIP.", m.chat.id, wait.message_id)
                            return
                    z.extractall(temp)

                items = os.listdir(temp)
                py_files = [x for x in items if x.endswith('.py')]
                js_files = [x for x in items if x.endswith('.js')]

                # Auto-install deps
                if 'requirements.txt' in items:
                    bot.edit_message_text("🐍 Installing Python deps...", m.chat.id, wait.message_id)
                    try:
                        r = subprocess.run(
                            [sys.executable, '-m', 'pip', 'install', '-r', os.path.join(temp, 'requirements.txt')],
                            capture_output=True, text=True, timeout=600
                        )
                        if r.returncode != 0:
                            logger.warning(f"pip warn: {(r.stderr or '')[:300]}")
                    except Exception as e:
                        logger.warning(f"pip error: {e}")

                if 'package.json' in items:
                    bot.edit_message_text("🟠 Installing Node deps...", m.chat.id, wait.message_id)
                    try:
                        r = subprocess.run(
                            ['npm', 'install'], cwd=temp,
                            capture_output=True, text=True, timeout=600
                        )
                        if r.returncode != 0:
                            logger.warning(f"npm warn: {(r.stderr or '')[:300]}")
                    except FileNotFoundError:
                        bot.reply_to(m, "⚠️ `npm` not found. Node.js required.")
                    except Exception as e:
                        logger.warning(f"npm error: {e}")

                # Pick main script
                main, ftype = None, None
                for candidate in ('main.py', 'bot.py', 'app.py'):
                    if candidate in py_files:
                        main, ftype = candidate, 'py'
                        break
                if not main:
                    for candidate in ('index.js', 'main.js', 'bot.js', 'app.js'):
                        if candidate in js_files:
                            main, ftype = candidate, 'js'
                            break
                if not main:
                    if py_files:
                        main, ftype = py_files[0], 'py'
                    elif js_files:
                        main, ftype = js_files[0], 'js'
                if not main:
                    bot.edit_message_text("❌ No `.py` or `.js` found in ZIP.", m.chat.id, wait.message_id)
                    return

                # Move everything into user folder
                for item in os.listdir(temp):
                    src = os.path.join(temp, item)
                    dst = os.path.join(folder, item)
                    if os.path.isdir(dst):
                        shutil.rmtree(dst, ignore_errors=True)
                    elif os.path.exists(dst):
                        try: os.remove(dst)
                        except Exception: pass
                    shutil.move(src, dst)

                save_user_file(uid, main, ftype)
                mp = os.path.join(folder, main)
                bot.edit_message_text(f"✅ Extracted. Starting `{main}`...",
                                      m.chat.id, wait.message_id, parse_mode='Markdown')

                if ftype == 'py':
                    threading.Thread(target=run_script,
                                     args=(mp, uid, folder, main, m)).start()
                else:
                    threading.Thread(target=run_js_script,
                                     args=(mp, uid, folder, main, m)).start()
            finally:
                shutil.rmtree(temp, ignore_errors=True)
            return

        # ───── Single file branch ─────
        path = os.path.join(folder, fn)
        with open(path, 'wb') as f:
            f.write(content)
        save_user_file(uid, fn, ext[1:])
        bot.edit_message_text(f"✅ Saved `{fn}`. Starting...",
                              m.chat.id, wait.message_id, parse_mode='Markdown')

        if ext == '.py':
            threading.Thread(target=run_script,
                             args=(path, uid, folder, fn, m)).start()
        else:
            threading.Thread(target=run_js_script,
                             args=(path, uid, folder, fn, m)).start()

    except Exception as e:
        logger.error(f"Upload error for {uid}: {e}", exc_info=True)
        bot.reply_to(m, f"❌ Error: {e}")

# ───────────────────────── CALLBACKS ─────────────────────────
def toggle_lock():
    global bot_locked
    bot_locked = not bot_locked
    return bot_locked

@bot.callback_query_handler(func=lambda call: True)
def handle_cb(call):
    uid = call.from_user.id
    data = call.data
    logger.info(f"CB: uid={uid} data={data}")

    if uid in banned_users and data != 'main':
        bot.answer_callback_query(call.id, "❌ You are banned.", show_alert=True)
        return
    if bot_locked and uid not in admin_ids and data not in ('main', 'speed', 'stats'):
        bot.answer_callback_query(call.id, "⚠️ Bot is locked.", show_alert=True)
        return

    try:
        if data == 'main':
            bot.answer_callback_query(call.id)
            try:
                bot.edit_message_reply_markup(call.message.chat.id,
                                              call.message.message_id,
                                              reply_markup=main_menu_inline(uid))
            except Exception:
                pass

        elif data == 'upload':
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id,
                             "📤 Send a `.py`, `.js`, or `.zip` file.",
                             parse_mode='Markdown')

        elif data == 'check_files':
            bot.answer_callback_query(call.id)
            files = user_files.get(uid, [])
            if not files:
                bot.send_message(call.message.chat.id, "📂 You have not uploaded any files yet.")
                return
            markup = types.InlineKeyboardMarkup(row_width=1)
            for fn, ft in sorted(files):
                icon = "🟢" if is_running(uid, fn) else "🔴"
                markup.add(types.InlineKeyboardButton(
                    f"{fn} ({ft}) {icon}", callback_data=f"file_{uid}_{fn}"))
            markup.add(types.InlineKeyboardButton("🔙 Back", callback_data='main'))
            try:
                bot.edit_message_text("📂 Your files:", call.message.chat.id,
                                      call.message.message_id, reply_markup=markup)
            except Exception:
                bot.send_message(call.message.chat.id, "📂 Your files:", reply_markup=markup)

        elif data.startswith('file_'):
            _, owner_s, *rest = data.split('_')
            owner = int(owner_s)
            fn = '_'.join(rest)
            if uid != owner and uid not in admin_ids:
                bot.answer_callback_query(call.id, "Not your file.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            running = is_running(owner, fn)
            ft = next((f[1] for f in user_files.get(owner, []) if f[0] == fn), '?')
            markup = types.InlineKeyboardMarkup(row_width=2)
            if running:
                markup.row(
                    types.InlineKeyboardButton("🔴 Stop", callback_data=f"stop_{owner}_{fn}"),
                    types.InlineKeyboardButton("🔄 Restart", callback_data=f"restart_{owner}_{fn}")
                )
            else:
                markup.row(types.InlineKeyboardButton("🟢 Start", callback_data=f"start_{owner}_{fn}"))
            markup.row(
                types.InlineKeyboardButton("📜 Logs", callback_data=f"logs_{owner}_{fn}"),
                types.InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_{owner}_{fn}")
            )
            markup.add(types.InlineKeyboardButton("🔙 Back", callback_data='check_files'))
            status = '🟢 Running' if running else '🔴 Stopped'
            try:
                bot.edit_message_text(f"⚙️ `{fn}` ({ft})\nStatus: {status}",
                                      call.message.chat.id, call.message.message_id,
                                      reply_markup=markup, parse_mode='Markdown')
            except Exception:
                pass

        elif data.startswith('start_'):
            _, owner_s, *rest = data.split('_')
            owner = int(owner_s)
            fn = '_'.join(rest)
            if uid != owner and uid not in admin_ids:
                bot.answer_callback_query(call.id, "Not authorized.", show_alert=True)
                return
            if is_running(owner, fn):
                bot.answer_callback_query(call.id, "Already running.", show_alert=True)
                return
            folder = get_user_folder(owner)
            path = os.path.join(folder, fn)
            if not os.path.exists(path):
                bot.answer_callback_query(call.id, "File missing.", show_alert=True)
                return
            ft = next((f[1] for f in user_files.get(owner, []) if f[0] == fn), 'py')
            bot.answer_callback_query(call.id, "Starting...")
            if ft == 'py':
                threading.Thread(target=run_script,
                                 args=(path, owner, folder, fn, call.message)).start()
            else:
                threading.Thread(target=run_js_script,
                                 args=(path, owner, folder, fn, call.message)).start()

        elif data.startswith('stop_'):
            _, owner_s, *rest = data.split('_')
            owner = int(owner_s)
            fn = '_'.join(rest)
            if uid != owner and uid not in admin_ids:
                bot.answer_callback_query(call.id, "Not authorized.", show_alert=True)
                return
            key = f"{owner}_{fn}"
            if key in bot_scripts:
                kill_tree(bot_scripts[key])
                bot_scripts.pop(key, None)
            bot.answer_callback_query(call.id, "Stopped.")
            try:
                bot.edit_message_text(
                    f"⚙️ `{fn}`\nStatus: 🔴 Stopped",
                    call.message.chat.id, call.message.message_id,
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton("🔙 Back", callback_data='check_files')
                    ),
                    parse_mode='Markdown'
                )
            except Exception:
                pass

        elif data.startswith('restart_'):
            _, owner_s, *rest = data.split('_')
            owner = int(owner_s)
            fn = '_'.join(rest)
            if uid != owner and uid not in admin_ids:
                bot.answer_callback_query(call.id, "Not authorized.", show_alert=True)
                return
            key = f"{owner}_{fn}"
            if key in bot_scripts:
                kill_tree(bot_scripts[key])
                bot_scripts.pop(key, None)
            time.sleep(1)
            folder = get_user_folder(owner)
            path = os.path.join(folder, fn)
            ft = next((f[1] for f in user_files.get(owner, []) if f[0] == fn), 'py')
            bot.answer_callback_query(call.id, "Restarting...")
            if ft == 'py':
                threading.Thread(target=run_script,
                                 args=(path, owner, folder, fn, call.message)).start()
            else:
                threading.Thread(target=run_js_script,
                                 args=(path, owner, folder, fn, call.message)).start()

        elif data.startswith('delete_'):
            _, owner_s, *rest = data.split('_')
            owner = int(owner_s)
            fn = '_'.join(rest)
            if uid != owner and uid not in admin_ids:
                bot.answer_callback_query(call.id, "Not authorized.", show_alert=True)
                return
            key = f"{owner}_{fn}"
            if key in bot_scripts:
                kill_tree(bot_scripts[key])
                bot_scripts.pop(key, None)
            folder = get_user_folder(owner)
            for p in (os.path.join(folder, fn),
                      os.path.join(folder, os.path.splitext(fn)[0] + '.log')):
                if os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass
            remove_user_file(owner, fn)
            bot.answer_callback_query(call.id, "Deleted.")
            try:
                bot.edit_message_text(f"🗑️ Deleted `{fn}`", call.message.chat.id,
                                      call.message.message_id, parse_mode='Markdown')
            except Exception:
                pass

        elif data.startswith('logs_'):
            _, owner_s, *rest = data.split('_')
            owner = int(owner_s)
            fn = '_'.join(rest)
            if uid != owner and uid not in admin_ids:
                bot.answer_callback_query(call.id, "Not authorized.", show_alert=True)
                return
            folder = get_user_folder(owner)
            log_path = os.path.join(folder, os.path.splitext(fn)[0] + '.log')
            if not os.path.exists(log_path):
                bot.answer_callback_query(call.id, "No logs yet.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            try:
                with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()[-3000:]
                bot.send_message(call.message.chat.id,
                                 f"📜 Logs for `{fn}`:\n```\n{content or '(empty)'}\n```",
                                 parse_mode='Markdown')
            except Exception as e:
                bot.send_message(call.message.chat.id, f"❌ {e}")

        elif data == 'speed':
            start = time.time()
            bot.answer_callback_query(call.id)
            latency = (time.time() - start) * 1000
            bot.send_message(call.message.chat.id,
                             f"⚡ Latency: {latency:.0f} ms\n"
                             f"🔒 Bot: {'Locked' if bot_locked else 'Unlocked'}\n"
                             f"👤 You: {status_of(uid)}")

        elif data == 'stats':
            bot.answer_callback_query(call.id)
            cmd_stats(call.message)

        elif data == 'sub_menu':
            if uid not in admin_ids:
                bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.row(
                types.InlineKeyboardButton('➕ Add', callback_data='add_sub'),
                types.InlineKeyboardButton('➖ Remove', callback_data='rem_sub')
            )
            markup.add(types.InlineKeyboardButton('🔙 Back', callback_data='main'))
            bot.edit_message_text("💳 Subscription Management",
                                  call.message.chat.id, call.message.message_id,
                                  reply_markup=markup)

        elif data == 'add_sub':
            if uid not in admin_ids: return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id,
                                   "Send `user_id days` (e.g. `123456789 30`)",
                                   parse_mode='Markdown')
            bot.register_next_step_handler(msg, do_add_sub)

        elif data == 'rem_sub':
            if uid not in admin_ids: return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "Send the user_id to remove.")
            bot.register_next_step_handler(msg, do_rem_sub)

        elif data == 'broadcast':
            if uid not in admin_ids: return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "📢 Send the message to broadcast.")
            bot.register_next_step_handler(msg, do_broadcast)

        elif data == 'lock_toggle':
            if uid not in admin_ids: return
            new_state = toggle_lock()
            bot.answer_callback_query(call.id, f"Bot {'locked 🔒' if new_state else 'unlocked 🔓'}.")

        elif data == 'admin_panel':
            if uid not in admin_ids: return
            bot.answer_callback_query(call.id)
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.row(
                types.InlineKeyboardButton('➕ Add Admin', callback_data='add_admin'),
                types.InlineKeyboardButton('➖ Remove Admin', callback_data='rem_admin')
            )
            markup.add(types.InlineKeyboardButton('📋 List Admins', callback_data='list_admins'))
            markup.add(types.InlineKeyboardButton('🔙 Back', callback_data='main'))
            bot.edit_message_text("👑 Admin Panel", call.message.chat.id,
                                  call.message.message_id, reply_markup=markup)

        elif data == 'add_admin':
            if uid != OWNER_ID:
                bot.answer_callback_query(call.id, "Owner only.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "Send user_id to promote.")
            bot.register_next_step_handler(msg, do_add_admin)

        elif data == 'rem_admin':
            if uid != OWNER_ID:
                bot.answer_callback_query(call.id, "Owner only.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "Send user_id to demote.")
            bot.register_next_step_handler(msg, do_rem_admin)

        elif data == 'list_admins':
            if uid not in admin_ids: return
            bot.answer_callback_query(call.id)
            text = "👑 Admins:\n" + "\n".join(
                f"- `{a}` {'(Owner)' if a == OWNER_ID else ''}" for a in sorted(admin_ids))
            bot.send_message(call.message.chat.id, text, parse_mode='Markdown')

        elif data == 'user_mgmt':
            if uid not in admin_ids: return
            bot.answer_callback_query(call.id)
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.row(
                types.InlineKeyboardButton('🚫 Ban', callback_data='ban'),
                types.InlineKeyboardButton('✅ Unban', callback_data='unban')
            )
            markup.row(
                types.InlineKeyboardButton('🔧 Set Limit', callback_data='set_limit'),
                types.InlineKeyboardButton('📊 User Info', callback_data='uinfo')
            )
            markup.row(types.InlineKeyboardButton('👥 All Users', callback_data='all_users'))
            markup.add(types.InlineKeyboardButton('🔙 Back', callback_data='main'))
            bot.edit_message_text("👥 User Management", call.message.chat.id,
                                  call.message.message_id, reply_markup=markup)

        elif data == 'ban':
            if uid not in admin_ids: return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "Send `user_id reason`")
            bot.register_next_step_handler(msg, do_ban)

        elif data == 'unban':
            if uid not in admin_ids: return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "Send user_id to unban.")
            bot.register_next_step_handler(msg, do_unban)

        elif data == 'set_limit':
            if uid not in admin_ids: return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "Send `user_id limit`")
            bot.register_next_step_handler(msg, do_set_limit)

        elif data == 'uinfo':
            if uid not in admin_ids: return
            bot.answer_callback_query(call.id)
            msg = bot.send_message(call.message.chat.id, "Send user_id for info.")
            bot.register_next_step_handler(msg, do_uinfo)

        elif data == 'all_users':
            if uid not in admin_ids: return
            bot.answer_callback_query(call.id)
            users = list(active_users)
            text = f"👥 All Users ({len(users)}):\n"
            text += "\n".join(f"- `{u}`" for u in users[:50])
            if len(users) > 50:
                text += f"\n... and {len(users) - 50} more"
            bot.send_message(call.message.chat.id, text, parse_mode='Markdown')

        elif data == 'run_all':
            if uid not in admin_ids: return
            bot.answer_callback_query(call.id, "Starting all scripts...")
            started = 0
            for owner, files in list(user_files.items()):
                for fn, ft in files:
                    if not is_running(owner, fn):
                        folder = get_user_folder(owner)
                        path = os.path.join(folder, fn)
                        if os.path.exists(path):
                            if ft == 'py':
                                threading.Thread(target=run_script,
                                                 args=(path, owner, folder, fn, call.message)).start()
                            else:
                                threading.Thread(target=run_js_script,
                                                 args=(path, owner, folder, fn, call.message)).start()
                            started += 1
                            time.sleep(0.5)
            bot.send_message(call.message.chat.id, f"✅ Started {started} script(s).")

        else:
            bot.answer_callback_query(call.id, "Unknown action.")

    except Exception as e:
        logger.error(f"Callback error: {e}", exc_info=True)
        try:
            bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)
        except Exception:
            pass

# ───────────────────────── STEP HANDLERS ─────────────────────────
def do_add_sub(m):
    if m.from_user.id not in admin_ids: return
    try:
        parts = m.text.split()
        sub_uid = int(parts[0])
        days = int(parts[1])
        current = user_subscriptions.get(sub_uid, {}).get('expiry')
        start = current if current and current > datetime.now() else datetime.now()
        new_expiry = start + timedelta(days=days)
        save_subscription(sub_uid, new_expiry)
        bot.reply_to(m, f"✅ Subscription for `{sub_uid}` until `{new_expiry:%Y-%m-%d}`",
                     parse_mode='Markdown')
        try:
            bot.send_message(sub_uid, f"🎉 Subscription extended by {days} days!")
        except Exception: pass
    except Exception as e:
        bot.reply_to(m, f"❌ Format: `user_id days` — {e}", parse_mode='Markdown')

def do_rem_sub(m):
    if m.from_user.id not in admin_ids: return
    try:
        sub_uid = int(m.text.strip())
        remove_subscription(sub_uid)
        bot.reply_to(m, f"✅ Removed subscription for `{sub_uid}`.", parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(m, f"❌ {e}")

def do_add_admin(m):
    if m.from_user.id != OWNER_ID: return
    try:
        new_uid = int(m.text.strip())
        with DB_LOCK:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            try:
                c = conn.cursor()
                c.execute('INSERT OR IGNORE INTO admins (user_id, added_by, added_date) VALUES (?, ?, ?)',
                          (new_uid, m.from_user.id, datetime.now().isoformat()))
                conn.commit()
            finally:
                conn.close()
        admin_ids.add(new_uid)
        bot.reply_to(m, f"✅ `{new_uid}` is now an admin.", parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(m, f"❌ {e}")

def do_rem_admin(m):
    if m.from_user.id != OWNER_ID: return
    try:
        tgt = int(m.text.strip())
        if tgt == OWNER_ID:
            bot.reply_to(m, "❌ Cannot remove owner.")
            return
        with DB_LOCK:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            try:
                c = conn.cursor()
                c.execute('DELETE FROM admins WHERE user_id=?', (tgt,))
                conn.commit()
            finally:
                conn.close()
        admin_ids.discard(tgt)
        bot.reply_to(m, f"✅ Removed admin `{tgt}`.", parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(m, f"❌ {e}")

def do_ban(m):
    if m.from_user.id not in admin_ids: return
    try:
        parts = m.text.split(maxsplit=1)
        tgt = int(parts[0])
        reason = parts[1] if len(parts) > 1 else "No reason"
        if tgt == OWNER_ID or tgt in admin_ids:
            bot.reply_to(m, "❌ Cannot ban owner/admin.")
            return
        ban_user(tgt, reason, m.from_user.id)
        for fn, _ in list(user_files.get(tgt, [])):
            key = f"{tgt}_{fn}"
            if key in bot_scripts:
                kill_tree(bot_scripts[key])
                bot_scripts.pop(key, None)
        bot.reply_to(m, f"✅ Banned `{tgt}` — {reason}", parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(m, f"❌ {e}")

def do_unban(m):
    if m.from_user.id not in admin_ids: return
    try:
        tgt = int(m.text.strip())
        unban_user(tgt)
        bot.reply_to(m, f"✅ Unbanned `{tgt}`.", parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(m, f"❌ {e}")

def do_set_limit(m):
    if m.from_user.id not in admin_ids: return
    try:
        parts = m.text.split()
        tgt = int(parts[0])
        limit = int(parts[1])
        set_user_limit(tgt, limit, m.from_user.id)
        bot.reply_to(m, f"✅ Limit for `{tgt}` set to {limit}.", parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(m, f"❌ {e}")

def do_uinfo(m):
    if m.from_user.id not in admin_ids: return
    try:
        tgt = int(m.text.strip())
        running = sum(1 for fn, _ in user_files.get(tgt, []) if is_running(tgt, fn))
        limit = get_user_file_limit(tgt)
        limit_str = str(limit) if limit != float('inf') else "Unlimited"
        text = (f"👤 `{tgt}`\n"
                f"Status: {status_of(tgt)}\n"
                f"Files: {get_user_file_count(tgt)}/{limit_str}\n"
                f"Running scripts: {running}")
        bot.reply_to(m, text, parse_mode='Markdown')
    except Exception as e:
        bot.reply_to(m, f"❌ {e}")

# ───────────────────────── SHUTDOWN ─────────────────────────
def cleanup():
    logger.warning("Shutting down — stopping scripts...")
    for key in list(bot_scripts.keys()):
        try:
            kill_tree(bot_scripts[key])
        except Exception:
            pass
    bot_scripts.clear()

atexit.register(cleanup)

# ───────────────────────── MAIN ─────────────────────────
if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("🤖 SHADOW HOSTING BOT starting")
    logger.info(f"🐍 Python: {sys.version.split()[0]}")
    logger.info(f"👑 Owner ID: {OWNER_ID}")
    logger.info(f"🛡️ Admins: {len(admin_ids)}")
    logger.info(f"🚫 Banned: {len(banned_users)}")
    logger.info("=" * 50)
    keep_alive()
    logger.info("🚀 Starting polling...")
    try:
        bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        pass
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)