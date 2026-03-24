import logging
import os
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

# ================= CONFIG =================
BOT_TOKEN   = os.getenv("BOT_TOKEN")
OWNER_SECRET = os.getenv("OWNER_SECRET", "secret123")
MONGO_URI   = os.getenv("MONGO_URI")
PORT        = int(os.getenv("PORT", 10000))

if not BOT_TOKEN:  raise ValueError("BOT_TOKEN missing")
if not MONGO_URI:  raise ValueError("MONGO_URI missing")

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ================= MONGODB =================
mongo        = AsyncIOMotorClient(MONGO_URI)
mdb          = mongo["multibot"]

# Main bot collections
main_users   = mdb["main_users"]       # {user_id, role, username, joined}
child_bots   = mdb["child_bots"]       # {token, username, name, owner_id, created, active}

# Per-bot collections are named: bot_{username}_{collection}
# e.g. bot_mybot_channels, bot_mybot_users, bot_mybot_settings, bot_mybot_broadcasts

def bot_col(bot_username: str, col: str):
    return mdb[f"bot_{bot_username}_{col}"]

# ================= WEB SERVER =================
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MultiBot Platform</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{min-height:100vh;background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);
display:flex;align-items:center;justify-content:center;font-family:'Segoe UI',sans-serif;color:#fff}
.card{background:rgba(255,255,255,.07);backdrop-filter:blur(12px);
border:1px solid rgba(255,255,255,.12);border-radius:24px;padding:50px 60px;
text-align:center;max-width:500px;width:90%;box-shadow:0 30px 60px rgba(0,0,0,.4)}
.dot{width:14px;height:14px;background:#00ff88;border-radius:50%;
display:inline-block;margin-right:8px;vertical-align:middle;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(0,255,136,.5)}50%{box-shadow:0 0 0 10px rgba(0,255,136,0)}}
h1{font-size:2rem;margin-bottom:8px}
.sub{color:rgba(255,255,255,.4);font-size:.9rem;margin-bottom:30px}
.badge{display:inline-block;background:rgba(0,255,136,.1);border:1px solid rgba(0,255,136,.3);
color:#00ff88;border-radius:50px;padding:10px 28px;font-size:1rem;font-weight:600;margin-bottom:28px}
.info{color:rgba(255,255,255,.3);font-size:.82rem;line-height:1.9}
.credit{margin-top:22px;font-size:.75rem;color:rgba(255,255,255,.2)}
</style>
</head>
<body>
<div class="card">
  <h1>&#x1F916; MultiBot Platform</h1>
  <p class="sub">Create and manage your Telegram bots</p>
  <div class="badge"><span class="dot"></span>SYSTEM ONLINE</div>
  <p class="info">All bots operational<br>MongoDB connected<br>Data persists across restarts</p>
  <div class="credit">&#x1F4AB; Powered by @aerivue</div>
</div>
</body>
</html>"""

class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML.encode("utf-8"))
    def log_message(self, *a): pass

def run_web():
    HTTPServer(("0.0.0.0", PORT), WebHandler).serve_forever()

# ================= MAIN BOT DB HELPERS =================
async def get_main_role(uid: int):
    u = await main_users.find_one({"user_id": uid})
    return u["role"] if u else None

async def is_main_owner(uid: int):
    return await get_main_role(uid) == "owner"

async def is_main_admin(uid: int):
    return await get_main_role(uid) in ("owner", "admin")

async def set_main_role(uid: int, role: str, username: str = ""):
    await main_users.update_one(
        {"user_id": uid},
        {"$set": {"role": role, "username": username, "updated": datetime.now()}},
        upsert=True
    )

async def save_main_user(uid: int, username: str = ""):
    await main_users.update_one(
        {"user_id": uid},
        {"$setOnInsert": {"user_id": uid, "username": username, "role": "user", "joined": datetime.now()}},
        upsert=True
    )

async def owner_exists():
    return await main_users.find_one({"role": "owner"}) is not None

# ================= CHILD BOT DB HELPERS =================
async def save_child_bot(token, username, name, owner_id):
    await child_bots.update_one(
        {"token": token},
        {"$set": {"token": token, "username": username, "name": name,
                  "owner_id": owner_id, "created": datetime.now(), "active": True}},
        upsert=True
    )

async def get_all_child_bots():
    return await child_bots.find({"active": True}).to_list(None)

async def deactivate_child_bot(username: str):
    await child_bots.update_one({"username": username}, {"$set": {"active": False}})

# ================= CHILD BOT HELPERS =================
async def cb_get_channels(uname):
    return await bot_col(uname, "channels").find({"active": True}).sort("number", 1).to_list(None)

async def cb_get_setting(uname, key, default=""):
    s = await bot_col(uname, "settings").find_one({"key": key})
    return s["value"] if s else default

async def cb_set_setting(uname, key, value):
    await bot_col(uname, "settings").update_one(
        {"key": key}, {"$set": {"key": key, "value": value}}, upsert=True
    )

async def cb_is_admin(uname, uid):
    r = await bot_col(uname, "admins").find_one({"user_id": uid})
    return r is not None

async def cb_is_owner(uname, uid):
    r = await bot_col(uname, "admins").find_one({"user_id": uid})
    return r and r.get("role") == "owner"

async def cb_save_user(uname, uid):
    await bot_col(uname, "users").update_one(
        {"user_id": uid},
        {"$setOnInsert": {"user_id": uid, "joined": datetime.now()}},
        upsert=True
    )

async def cb_get_users(uname):
    return await bot_col(uname, "users").find().to_list(None)

async def cb_build_keyboard(channels, cols=2):
    keyboard, row = [], []
    for ch in channels:
        row.append(InlineKeyboardButton(f"CHANNEL {ch['number']}", url=ch["link"]))
        if len(row) == cols:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return keyboard

async def cb_is_joined_all(bot_obj, uname, uid):
    channels = await cb_get_channels(uname)
    if not channels:
        return False
    for ch in channels:
        try:
            username = ch["link"].split("/")[-1].replace("@", "").strip()
            member = await bot_obj.get_chat_member(f"@{username}", uid)
            if member.status in ["left", "kicked"]:
                return False
        except Exception:
            return False
    return True

async def cb_force_join(update, uname):
    channels = await cb_get_channels(uname)
    keyboard = await cb_build_keyboard(channels, cols=2)
    keyboard.append([InlineKeyboardButton("CHECK JOINED", callback_data="check")])
    markup = InlineKeyboardMarkup(keyboard)
    msg_text  = await cb_get_setting(uname, "force_msg", "Join all channels first!")
    image_url = await cb_get_setting(uname, "force_image", "")
    if image_url:
        await update.message.reply_photo(photo=image_url, caption=msg_text, reply_markup=markup)
    else:
        await update.message.reply_text(msg_text, reply_markup=markup)

async def cb_guard(update, context, uname):
    uid = update.effective_user.id
    if await cb_is_admin(uname, uid):
        return True
    if not await cb_is_joined_all(context.bot, uname, uid):
        await cb_force_join(update, uname)
        return False
    return True

# ================= CHILD BOT SETUP =================
def setup_child_bot(app: Application, uname: str, owner_id: int):

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        await cb_save_user(uname, uid)
        if not await cb_guard(update, context, uname):
            return
        if await cb_is_owner(uname, uid):
            await update.message.reply_text(
                "*Owner Panel*\n\n"
                "/add 1 link - Add channel\n"
                "/remove 1 - Remove channel\n"
                "/list - List channels\n"
                "/broadcast msg - Broadcast\n"
                "/setmsg text - Set force message\n"
                "/setimage url - Set force image\n"
                "/addadmin uid - Add admin\n"
                "/removeadmin uid - Remove admin\n"
                "/admins - List admins\n"
                "/stats - Statistics\n\n"
                "_Powered by @aerivuebot_",
                parse_mode="Markdown"
            )
        elif await cb_is_admin(uname, uid):
            await update.message.reply_text(
                "*Admin Panel*\n\n"
                "/add 1 link - Add channel\n"
                "/remove 1 - Remove channel\n"
                "/list - List channels\n"
                "/broadcast msg - Broadcast\n"
                "/setmsg text - Set force message\n"
                "/setimage url - Set force image\n"
                "/stats - Statistics\n\n"
                "_Powered by @aerivuebot_",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("Access Granted!")

    async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        if await cb_is_joined_all(context.bot, uname, q.from_user.id):
            await q.edit_message_text("Verified! Use /start to continue.")
        else:
            await q.answer("Join all channels first!", show_alert=True)

    async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await cb_is_admin(uname, update.effective_user.id): return
        try:
            n, l = int(context.args[0]), context.args[1]
            await bot_col(uname, "channels").update_one(
                {"number": n},
                {"$set": {"number": n, "link": l, "active": True}},
                upsert=True
            )
            await update.message.reply_text(f"Channel {n} added!")
        except Exception:
            await update.message.reply_text("Usage: /add 1 https://t.me/channel")

    async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await cb_is_admin(uname, update.effective_user.id): return
        try:
            n = int(context.args[0])
            await bot_col(uname, "channels").update_one({"number": n}, {"$set": {"active": False}})
            await update.message.reply_text(f"Channel {n} removed!")
        except Exception:
            await update.message.reply_text("Usage: /remove 1")

    async def update_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await cb_is_admin(uname, update.effective_user.id): return
        try:
            n, l = int(context.args[0]), context.args[1]
            await bot_col(uname, "channels").update_one(
                {"number": n}, {"$set": {"link": l, "active": True}}, upsert=True
            )
            await update.message.reply_text(f"Channel {n} updated!")
        except Exception:
            await update.message.reply_text("Usage: /update 1 https://t.me/newlink")

    async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await cb_guard(update, context, uname): return
        chs = await cb_get_channels(uname)
        if not chs:
            await update.message.reply_text("No channels added yet.")
            return
        msg = "*Channels:*\n\n" + "\n".join([f"`{c['number']}` - {c['link']}" for c in chs])
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def set_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await cb_is_admin(uname, update.effective_user.id): return
        msg = " ".join(context.args).strip()
        if not msg:
            await update.message.reply_text("Usage: /setmsg Your message")
            return
        await cb_set_setting(uname, "force_msg", msg)
        await update.message.reply_text(f"Force message updated!\n\n{msg}")

    async def set_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await cb_is_admin(uname, update.effective_user.id): return
        url = " ".join(context.args).strip()
        if not url:
            await update.message.reply_text("Usage: /setimage https://...")
            return
        await cb_set_setting(uname, "force_image", url)
        await update.message.reply_text("Force image updated!")

    async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await cb_is_admin(uname, update.effective_user.id): return
        msg = " ".join(context.args).strip()
        if not msg:
            await update.message.reply_text("Usage: /broadcast message")
            return
        users = await cb_get_users(uname)
        ok, fail = 0, 0
        for u in users:
            try:
                await context.bot.send_message(chat_id=u["user_id"], text=msg)
                ok += 1
            except Exception:
                fail += 1
        await bot_col(uname, "broadcasts").insert_one({"msg": msg, "date": datetime.now(), "sent": ok, "failed": fail})
        await update.message.reply_text(f"Broadcast done!\n\nSent: {ok}\nFailed: {fail}")

    async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await cb_is_owner(uname, update.effective_user.id): return
        try:
            uid = int(context.args[0])
            await bot_col(uname, "admins").update_one(
                {"user_id": uid}, {"$set": {"user_id": uid, "role": "admin"}}, upsert=True
            )
            await update.message.reply_text(f"Admin {uid} added!")
        except Exception:
            await update.message.reply_text("Usage: /addadmin user_id")

    async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await cb_is_owner(uname, update.effective_user.id): return
        try:
            uid = int(context.args[0])
            if await cb_is_owner(uname, uid):
                await update.message.reply_text("Cannot remove owner!")
                return
            await bot_col(uname, "admins").delete_one({"user_id": uid})
            await update.message.reply_text(f"Admin {uid} removed!")
        except Exception:
            await update.message.reply_text("Usage: /removeadmin user_id")

    async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await cb_is_admin(uname, update.effective_user.id): return
        admins = await bot_col(uname, "admins").find().to_list(None)
        if not admins:
            await update.message.reply_text("No admins.")
            return
        msg = "*Admins:*\n\n" + "\n".join([f"- `{a['user_id']}` {a.get('role','admin').upper()}" for a in admins])
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await cb_is_admin(uname, update.effective_user.id): return
        ch = await bot_col(uname, "channels").count_documents({"active": True})
        us = await bot_col(uname, "users").count_documents({})
        ad = await bot_col(uname, "admins").count_documents({})
        br = await bot_col(uname, "broadcasts").count_documents({})
        await update.message.reply_text(
            f"*Bot Stats*\n\n"
            f"Channels: {ch}\nUsers: {us}\nAdmins: {ad}\nBroadcasts: {br}\n"
            f"Status: Online\n\n_Powered by @aerivuebot_",
            parse_mode="Markdown"
        )

    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("add",         add_channel))
    app.add_handler(CommandHandler("remove",      remove_channel))
    app.add_handler(CommandHandler("update",      update_channel))
    app.add_handler(CommandHandler("list",        list_channels))
    app.add_handler(CommandHandler("setmsg",      set_message))
    app.add_handler(CommandHandler("setimage",    set_image))
    app.add_handler(CommandHandler("broadcast",   broadcast))
    app.add_handler(CommandHandler("addadmin",    add_admin))
    app.add_handler(CommandHandler("removeadmin", remove_admin))
    app.add_handler(CommandHandler("admins",      list_admins))
    app.add_handler(CommandHandler("stats",       stats))
    app.add_handler(CallbackQueryHandler(check_join, pattern="check"))

# ================= BOT RUNNER =================
running_bots = {}

async def launch_child_bot(token: str, uname: str, owner_id: int):
    if token in running_bots:
        return
    try:
        app = Application.builder().token(token).build()
        setup_child_bot(app, uname, owner_id)
        running_bots[token] = app
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.warning(f"Child bot @{uname} started")
    except Exception as e:
        logger.error(f"Failed to launch child bot @{uname}: {e}")

async def stop_child_bot(token: str):
    app = running_bots.pop(token, None)
    if app:
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception as e:
            logger.error(f"Error stopping bot: {e}")

async def restore_child_bots():
    bots = await get_all_child_bots()
    for b in bots:
        await launch_child_bot(b["token"], b["username"], b["owner_id"])

# ================= MAIN BOT HANDLERS =================

async def main_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username or ""
    await save_main_user(uid, uname)
    role = await get_main_role(uid)

    if role == "owner":
        await update.message.reply_text(
            "*System Owner Panel*\n\n"
            "/addbot token - Add new child bot\n"
            "/removebotadmin @username - Remove a bot\n"
            "/listbots - List all bots\n"
            "/systembroadcast msg - Broadcast to ALL users\n"
            "/addadmin uid - Add main admin\n"
            "/removeadmin uid - Remove main admin\n"
            "/admins - List main admins\n"
            "/stats - System stats\n\n"
            "_Powered by @aerivuebot_",
            parse_mode="Markdown"
        )
    elif role == "admin":
        await update.message.reply_text(
            "*Main Admin Panel*\n\n"
            "/addbot token - Add new child bot\n"
            "/listbots - List all bots\n"
            "/systembroadcast msg - Broadcast to ALL users\n"
            "/stats - System stats\n\n"
            "_Powered by @aerivuebot_",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "*Welcome to MultiBot Platform*\n\n"
            "Create your own Telegram Permotion Bot for free!\n\n"
            "How to get started:\n"
            "1. Create a bot via @BotFather\n"
            "2. Send: /addbot YOUR_BOT_TOKEN\n"
            "3. Your bot is live instantly!\n"
            "4. See Your All Bots /listbots\n\n"
            "_Powered by @aerivue_",
            parse_mode="Markdown"
        )

async def main_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username or ""

    if await owner_exists():
        if await is_main_owner(uid):
            await update.message.reply_text("You are already the owner!")
        else:
            await update.message.reply_text("Owner already set.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /owner <secret>")
        return

    if context.args[0] != OWNER_SECRET:
        await update.message.reply_text("Wrong secret!")
        return

    await set_main_role(uid, "owner", uname)
    await update.message.reply_text(
        f"*You are now SYSTEM OWNER!*\n\n"
        f"Use /start to see all commands.\n\n"
        f"_Powered by @aerivuebot_",
        parse_mode="Markdown"
    )

async def main_addbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username or str(uid)

    if not await is_main_admin(uid):
        # Any user can create their own bot
        pass

    if not context.args:
        await update.message.reply_text("Usage: /addbot YOUR_BOT_TOKEN")
        return

    token = context.args[0].strip()
    await update.message.reply_text("Verifying token...")

    try:
        test_app = Application.builder().token(token).build()
        await test_app.initialize()
        me = await test_app.bot.get_me()
        await test_app.shutdown()

        bot_uname = me.username
        bot_name  = me.first_name

        # Check duplicate
        existing = await child_bots.find_one({"username": bot_uname, "active": True})
        if existing:
            await update.message.reply_text(f"Bot @{bot_uname} already exists!")
            return

        # Save to DB with owner as the user who added it
        await save_child_bot(token, bot_uname, bot_name, uid)

        # Set the user as owner of this child bot
        await bot_col(bot_uname, "admins").update_one(
            {"user_id": uid},
            {"$set": {"user_id": uid, "role": "owner"}},
            upsert=True
        )

        # Also add main system owner as admin in child bot
        sys_owner = await main_users.find_one({"role": "owner"})
        if sys_owner and sys_owner["user_id"] != uid:
            await bot_col(bot_uname, "admins").update_one(
                {"user_id": sys_owner["user_id"]},
                {"$set": {"user_id": sys_owner["user_id"], "role": "owner"}},
                upsert=True
            )

        # Launch the bot
        await launch_child_bot(token, bot_uname, uid)

        await update.message.reply_text(
            f"*Bot Created Successfully!*\n\n"
            f"Bot: @{bot_uname}\n"
            f"Name: {bot_name}\n\n"
            f"Your bot is now LIVE!\n"
            f"Go to @{bot_uname} and use /start\n\n"
            f"_Powered by @aerivuebot_",
            parse_mode="Markdown"
        )

    except Exception as e:
        await update.message.reply_text(f"Failed! Invalid token or bot already running.\n\nError: {str(e)[:100]}")

async def main_removebot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_main_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /removebotadmin @username")
        return

    uname = context.args[0].replace("@", "").strip()
    bot_doc = await child_bots.find_one({"username": uname})

    if not bot_doc:
        await update.message.reply_text(f"Bot @{uname} not found.")
        return

    await stop_child_bot(bot_doc["token"])
    await deactivate_child_bot(uname)
    await update.message.reply_text(f"Bot @{uname} removed and stopped!")

async def main_listbots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_main_admin(update.effective_user.id):
        # Show user their own bots
        uid = update.effective_user.id
        bots = await child_bots.find({"owner_id": uid, "active": True}).to_list(None)
        if not bots:
            await update.message.reply_text("You have no bots.\n\nUse /addbot token to create one!")
            return
        msg = "*Your Bots:*\n\n"
        for b in bots:
            status = "Online" if b["token"] in running_bots else "Offline"
            msg += f"@{b['username']} - {b['name']} [{status}]\n"
        await update.message.reply_text(msg + "\n_Powered by @aerivuebot_", parse_mode="Markdown")
        return

    bots = await get_all_child_bots()
    if not bots:
        await update.message.reply_text("No bots created yet.")
        return

    msg = "*All Bots:*\n\n"
    for b in bots:
        status = "Online" if b["token"] in running_bots else "Offline"
        users_count = await bot_col(b["username"], "users").count_documents({})
        msg += (
            f"@{b['username']} [{status}]\n"
            f"   Name: {b['name']}\n"
            f"   Owner: `{b['owner_id']}`\n"
            f"   Users: {users_count}\n\n"
        )
    await update.message.reply_text(msg + "_Powered by @aerivuebot_", parse_mode="Markdown")

async def main_sysbroadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_main_admin(update.effective_user.id):
        return
    msg = " ".join(context.args).strip()
    if not msg:
        await update.message.reply_text("Usage: /systembroadcast message")
        return

    await update.message.reply_text("Broadcasting to ALL users across ALL bots...")

    total_ok, total_fail = 0, 0
    bots = await get_all_child_bots()

    for b in bots:
        users = await cb_get_users(b["username"])
        app = running_bots.get(b["token"])
        if not app:
            continue
        for u in users:
            try:
                await app.bot.send_message(chat_id=u["user_id"], text=msg)
                total_ok += 1
            except Exception:
                total_fail += 1

    await update.message.reply_text(
        f"System Broadcast Done!\n\nSent: {total_ok}\nFailed: {total_fail}\n\n_Powered by @aerivuebot_",
        parse_mode="Markdown"
    )

async def main_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_main_owner(update.effective_user.id):
        return
    try:
        uid = int(context.args[0])
        await set_main_role(uid, "admin")
        await update.message.reply_text(f"Admin {uid} added!")
    except Exception:
        await update.message.reply_text("Usage: /addadmin user_id")

async def main_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_main_owner(update.effective_user.id):
        return
    try:
        uid = int(context.args[0])
        if await is_main_owner(uid):
            await update.message.reply_text("Cannot remove owner!")
            return
        await main_users.update_one({"user_id": uid}, {"$set": {"role": "user"}})
        await update.message.reply_text(f"Admin {uid} removed!")
    except Exception:
        await update.message.reply_text("Usage: /removeadmin user_id")

async def main_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_main_admin(update.effective_user.id):
        return
    admins = await main_users.find({"role": {"$in": ["owner", "admin"]}}).to_list(None)
    if not admins:
        await update.message.reply_text("No admins.")
        return
    msg = "*Main Admins:*\n\n"
    for a in admins:
        uname = f"@{a['username']}" if a.get("username") else ""
        msg += f"- `{a['user_id']}` {uname} - {a['role'].upper()}\n"
    await update.message.reply_text(msg + "\n_Powered by @aerivuebot_", parse_mode="Markdown")

async def main_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_main_admin(update.effective_user.id):
        return
    total_bots  = await child_bots.count_documents({"active": True})
    total_users = await main_users.count_documents({})
    running     = len(running_bots)

    total_bot_users = 0
    bots = await get_all_child_bots()
    for b in bots:
        total_bot_users += await bot_col(b["username"], "users").count_documents({})

    await update.message.reply_text(
        f"*System Stats*\n\n"
        f"Total Bots: {total_bots}\n"
        f"Running Bots: {running}\n"
        f"Main Users: {total_users}\n"
        f"Total Bot Users: {total_bot_users}\n"
        f"Status: Online\n\n"
        f"_Powered by @aerivuebot_",
        parse_mode="Markdown"
    )

# ================= MAIN =================
async def post_init(app: Application):
    await restore_child_bots()

def main():
    Thread(target=run_web, daemon=True).start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",           main_start))
    app.add_handler(CommandHandler("owner",           main_owner))
    app.add_handler(CommandHandler("addbot",          main_addbot))
    app.add_handler(CommandHandler("removebotadmin",  main_removebot))
    app.add_handler(CommandHandler("listbots",        main_listbots))
    app.add_handler(CommandHandler("systembroadcast", main_sysbroadcast))
    app.add_handler(CommandHandler("addadmin",        main_addadmin))
    app.add_handler(CommandHandler("removeadmin",     main_removeadmin))
    app.add_handler(CommandHandler("admins",          main_admins))
    app.add_handler(CommandHandler("stats",           main_stats))

    print("System starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
