import json
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8997777118:AAH8B0-0lPGw4HDzvZKL6bwLT9j3uHebep8")

# THREE ADMINS - already configured directly in this code.
MAIN_ADMIN_ID = 8076922044
NORMAL_ADMIN_IDS = {8842586144, 8771869393}
ADMIN_IDS = {MAIN_ADMIN_ID} | NORMAL_ADMIN_IDS

DATA_FILE = "bot_data.json"

# 9 optional channel slots.
# Existing 3 channels are kept. Slots 4-6 are public by default,
# slots 7-9 are private by default. Empty slots are ignored.
DEFAULT_CHANNELS = [
    {"id": -1004440630937, "link": "https://t.me/+-UWY1BhkkyMwMDhl", "type": "private"},
    {"id": -1003985546555, "link": "https://t.me/+Inz16JPrLCFjNTZl", "type": "private"},
    {"id": "@rocks_update", "link": "https://t.me/rocks_update", "type": "public"},
    {"id": "", "link": "", "type": "public"},
    {"id": "", "link": "", "type": "public"},
    {"id": "", "link": "", "type": "public"},
    {"id": "", "link": "", "type": "private"},
    {"id": "", "link": "", "type": "private"},
    {"id": "", "link": "", "type": "private"},
]

def default_data():
    return {
        "username": "",
        "password": "",
        "key": "",
        "users": [],
        "channels": DEFAULT_CHANNELS,
    }

def normalize_channels(channels):
    result = []
    for i in range(9):
        item = channels[i] if isinstance(channels, list) and i < len(channels) and isinstance(channels[i], dict) else {}
        ctype = item.get("type", DEFAULT_CHANNELS[i]["type"])
        if ctype not in ("public", "private"):
            ctype = DEFAULT_CHANNELS[i]["type"]
        result.append({
            "id": item.get("id", ""),
            "link": item.get("link", ""),
            "type": ctype,
        })
    return result

def save_data(data):
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        print("SAVE ERROR:", repr(e), flush=True)

def load_data():
    if not os.path.exists(DATA_FILE):
        data = default_data()
        save_data(data)
        return data
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = default_data()
        base = default_data()
        for key, value in base.items():
            data.setdefault(key, value)
        if not isinstance(data.get("users"), list):
            data["users"] = []
        data["channels"] = normalize_channels(data.get("channels"))
        return data
    except Exception as e:
        print("DATABASE ERROR:", repr(e), flush=True)
        return default_data()

data = load_data()
pending = {}
join_requests = set()

# ============================================================
# TELEGRAM API - NO EXTERNAL PACKAGES
# ============================================================
def api(method, payload=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    body = urllib.parse.urlencode(payload or {}).encode()
    req = urllib.request.Request(url, data=body)
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print(f"TELEGRAM API ERROR [{method}]: {e}", flush=True)
        return {"ok": False, "description": str(e)}

def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return api("sendMessage", payload)

def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return api("editMessageText", payload)

def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    return api("answerCallbackQuery", payload)

def create_private_invite_link(channel_id):
    """Create a fresh permanent join-request invite link."""
    result = api("createChatInviteLink", {
        "chat_id": channel_id,
        "creates_join_request": "true",
    })
    if not result.get("ok"):
        return None, result.get("description", "Unable to create invite link.")
    link = result.get("result", {}).get("invite_link")
    if not link:
        return None, "Telegram did not return an invite link."
    return link, None

# ============================================================
# CHANNEL HELPERS
# ============================================================
def is_admin(user_id):
    return int(user_id) in ADMIN_IDS

def configured_channels():
    result = []
    for index, channel in enumerate(data["channels"]):
        if str(channel.get("id", "")).strip() and str(channel.get("link", "")).strip():
            result.append((index, channel))
    return result

def is_member(user_id, channel_id):
    result = api("getChatMember", {"chat_id": channel_id, "user_id": user_id})
    if not result.get("ok"):
        print(
            f"MEMBERSHIP CHECK FAILED [{channel_id}]: "
            f"{result.get('description')}",
            flush=True,
        )
        return False
    status = result.get("result", {}).get("status")
    return status in ("member", "administrator", "creator")

def has_join_request(user_id, channel_id):
    return (str(channel_id), int(user_id)) in join_requests

def check_channel(user_id, channel):
    if is_member(user_id, channel["id"]):
        return True
    if channel["type"] == "private":
        return has_join_request(user_id, channel["id"])
    return False

def get_channel_statuses(user_id, channels):
    """
    Check all configured channels concurrently.
    This removes the old 9 x sequential API delay and makes /start and
    VERIFY much faster while keeping the same verification rules.
    """
    if not channels:
        return []

    results = [False] * len(channels)
    workers = min(9, len(channels))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(check_channel, user_id, channel): pos
            for pos, (_, channel) in enumerate(channels)
        }
        for future in as_completed(futures):
            pos = futures[future]
            try:
                results[pos] = bool(future.result())
            except Exception as exc:
                print(f"CHANNEL CHECK ERROR [{pos}]: {exc}", flush=True)
                results[pos] = False

    return results

def user_channel_keyboard(user_id, channels=None, statuses=None):
    channels = channels if channels is not None else configured_channels()
    if statuses is None:
        statuses = get_channel_statuses(user_id, channels)

    rows = []
    for pos, (index, channel) in enumerate(channels):
        if pos >= len(statuses) or not statuses[pos]:
            rows.append([{
                "text": f"𝐂𝐇𝐀𝐍𝐍𝐄𝐋 {index + 1}",
                "url": channel["link"],
                "style": "primary",
            }])

    if rows:
        rows.append([{
            "text": "𝐕𝐄𝐑𝐈𝐅𝐘",
            "callback_data": "verify",
            "style": "success",
        }])
    return {"inline_keyboard": rows}

def credentials_ready():
    return bool(data.get("username") and data.get("password") and data.get("key"))

def credentials_text():
    return (
        "🎉 <b>VERIFICATION SUCCESSFUL!</b>\n\n"
        "You have joined all required channels.\n\n"
        f"👤 <b>USERNAME</b>\n<code>{data['username']}</code>\n\n"
        f"🔐 <b>PASSWORD</b>\n<code>{data['password']}</code>\n\n"
        f"🔑 <b>KEY</b>\n<code>{data['key']}</code>\n\n"
        "♾️ Unlimited Access"
    )

def save_user(user_id):
    if user_id not in data["users"]:
        data["users"].append(user_id)
        save_data(data)

def start(chat_id, user_id):
    save_user(user_id)
    channels = configured_channels()

    if not channels:
        if credentials_ready():
            send_message(chat_id, credentials_text())
        else:
            send_message(
                chat_id,
                "👋 <b>WELCOME</b>\n\n"
                "No channels are currently configured. Please try again later.",
            )
        return

    statuses = get_channel_statuses(user_id, channels)

    # Already joined all configured channels -> no channel buttons.
    if all(statuses):
        if credentials_ready():
            send_message(chat_id, credentials_text())
        else:
            send_message(
                chat_id,
                "⚠️ <b>CREDENTIALS NOT AVAILABLE</b>\n\n"
                "The administrator has not configured the username, password and key yet.",
            )
        return

    missing = sum(not status for status in statuses)
    send_message(
        chat_id,
        f"🎯 <b>ACCESS VERIFICATION</b>\n\n"
        f"{missing} required channel(s) are still missing.\n\n"
        "Join the channel(s) below and press <b>𝐕𝐄𝐑𝐈𝐅𝐘</b>.",
        user_channel_keyboard(user_id, channels, statuses),
    )

def verify_callback(query):
    answer_callback(query["id"])
    user_id = query["from"]["id"]
    message = query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    if chat_id is None or message_id is None:
        return

    channels = configured_channels()

    if not channels:
        edit_message(
            chat_id,
            message_id,
            "👋 <b>NO CHANNELS CONFIGURED</b>\n\nNo channel has been added yet.",
        )
        return

    statuses = get_channel_statuses(user_id, channels)

    if all(statuses):
        if credentials_ready():
            edit_message(chat_id, message_id, credentials_text())
        else:
            edit_message(
                chat_id,
                message_id,
                "⚠️ <b>CREDENTIALS NOT AVAILABLE</b>\n\n"
                "The administrator has not configured the username, password and key yet.",
            )
        return

    missing = sum(not status for status in statuses)
    edit_message(
        chat_id,
        message_id,
        f"❌ <b>VERIFICATION INCOMPLETE</b>\n\n"
        f"{missing} required channel(s) are still missing.\n\n"
        "Join the channel(s) shown below and press <b>𝐕𝐄𝐑𝐈𝐅𝐘</b> again.",
        user_channel_keyboard(user_id, channels, statuses),
    )

# ============================================================
# ADMIN PANEL
# ============================================================
def normal_admin_keyboard(user_id):
    return {"inline_keyboard": [
        [{"text": "𝐒𝐄𝐓 𝐔𝐒𝐄𝐑𝐍𝐀𝐌𝐄", "callback_data": "set_username", "style": "primary"}],
        [{"text": "𝐒𝐄𝐓 𝐏𝐀𝐒𝐒𝐖𝐎𝐑𝐃", "callback_data": "set_password", "style": "primary"}],
        [{"text": "𝐑𝐄𝐌𝐎𝐕𝐄 𝐔𝐒𝐄𝐑𝐍𝐀𝐌𝐄", "callback_data": "remove_username", "style": "danger"}],
        [{"text": "𝐑𝐄𝐌𝐎𝐕𝐄 𝐏𝐀𝐒𝐒𝐖𝐎𝐑𝐃", "callback_data": "remove_password", "style": "danger"}],
        [{"text": "𝐔𝐒𝐀𝐆𝐄 / 𝐒𝐓𝐀𝐓𝐒", "callback_data": "usage", "style": "success"}],
    ]}

def main_admin_keyboard(user_id):
    return {"inline_keyboard": [
        [{"text": "𝐒𝐄𝐓 𝐔𝐒𝐄𝐑𝐍𝐀𝐌𝐄", "callback_data": "set_username", "style": "primary"}],
        [{"text": "𝐒𝐄𝐓 𝐏𝐀𝐒𝐒𝐖𝐎𝐑𝐃", "callback_data": "set_password", "style": "primary"}],
        [{"text": "𝐒𝐄𝐓 𝐊𝐄𝐘", "callback_data": "set_key", "style": "primary"}],
        [{"text": "𝐑𝐄𝐌𝐎𝐕𝐄 𝐂𝐑𝐄𝐃𝐄𝐍𝐓𝐈𝐀𝐋𝐒", "callback_data": "remove_credentials", "style": "danger"}],
        [{"text": "𝐀𝐃𝐃 𝐏𝐔𝐁𝐋𝐈𝐂 𝐂𝐇𝐀𝐍𝐍𝐄𝐋", "callback_data": "add_public", "style": "primary"}],
        [{"text": "𝐀𝐃𝐃 𝐏𝐑𝐈𝐕𝐀𝐓𝐄 𝐂𝐇𝐀𝐍𝐍𝐄𝐋", "callback_data": "add_private", "style": "primary"}],
        [{"text": "𝐂𝐇𝐀𝐍𝐍𝐄𝐋 𝟏", "callback_data": "view_1", "style": "primary"}, {"text": "𝐂𝐇𝐀𝐍𝐍𝐄𝐋 𝟐", "callback_data": "view_2", "style": "primary"}, {"text": "𝐂𝐇𝐀𝐍𝐍𝐄𝐋 𝟑", "callback_data": "view_3", "style": "primary"}],
        [{"text": "𝐂𝐇𝐀𝐍𝐍𝐄𝐋 𝟒", "callback_data": "view_4", "style": "primary"}, {"text": "𝐂𝐇𝐀𝐍𝐍𝐄𝐋 𝟓", "callback_data": "view_5", "style": "primary"}, {"text": "𝐂𝐇𝐀𝐍𝐍𝐄𝐋 𝟔", "callback_data": "view_6", "style": "primary"}],
        [{"text": "𝐂𝐇𝐀𝐍𝐍𝐄𝐋 𝟕", "callback_data": "view_7", "style": "primary"}, {"text": "𝐂𝐇𝐀𝐍𝐍𝐄𝐋 𝟖", "callback_data": "view_8", "style": "primary"}, {"text": "𝐂𝐇𝐀𝐍𝐍𝐄𝐋 𝟗", "callback_data": "view_9", "style": "primary"}],
        [{"text": "𝐔𝐒𝐀𝐆𝐄 / 𝐒𝐓𝐀𝐓𝐒", "callback_data": "usage", "style": "success"}],
    ]}

def admin_keyboard(user_id):
    return main_admin_keyboard(user_id) if int(user_id) == MAIN_ADMIN_ID else normal_admin_keyboard(user_id)

def slot_keyboard(action, start=0):
    # Six slots are shown per page to keep the panel compact.
    end = min(start + 6, 9)
    rows = []
    current = []
    for i in range(start, end):
        current.append({
            "text": f"𝐂𝐇𝐀𝐍𝐍𝐄𝐋 {i + 1}",
            "callback_data": f"{action}_{i}",
            "style": "primary",
        })
        if len(current) == 3:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    if start == 0 and end < 9:
        rows.append([{"text": "𝐌𝐎𝐑𝐄 𝐒𝐋𝐎𝐓𝐒", "callback_data": f"{action}_page_6", "style": "success"}])
    elif start == 6:
        rows.append([{"text": "𝐁𝐀𝐂𝐊 𝐓𝐎 𝟏-𝟔", "callback_data": f"{action}_page_0", "style": "success"}])
    rows.append([{"text": "𝐁𝐀𝐂𝐊", "callback_data": "admin_back", "style": "success"}])
    return {"inline_keyboard": rows}

def channel_info_keyboard(index):
    rows = []
    if data["channels"][index].get("type") == "private":
        rows.append([{
            "text": "𝐑𝐄𝐅𝐑𝐄𝐒𝐇 𝐏𝐑𝐈𝐕𝐀𝐓𝐄 𝐋𝐈𝐍𝐊",
            "callback_data": f"refresh_{index}",
            "style": "primary",
        }])
    rows.append([{
        "text": "𝐑𝐄𝐌𝐎𝐕𝐄 𝐂𝐇𝐀𝐍𝐍𝐄𝐋",
        "callback_data": f"remove_{index}",
        "style": "danger",
    }])
    rows.append([{"text": "𝐁𝐀𝐂𝐊", "callback_data": "admin_back", "style": "success"}])
    return {"inline_keyboard": rows}

def channel_info(index):
    channel = data["channels"][index]
    if not str(channel.get("id", "")).strip() or not str(channel.get("link", "")).strip():
        return f"𝐂𝐇𝐀𝐍𝐍𝐄𝐋 {index + 1}\n\nStatus: <b>NOT CONFIGURED</b>"
    return (
        f"𝐂𝐇𝐀𝐍𝐍𝐄𝐋 {index + 1}\n\n"
        f"Type: <b>{channel['type'].upper()}</b>\n"
        f"ID: <code>{channel['id']}</code>\n"
        f"Link: {channel['link']}"
    )

def admin_panel(chat_id, user_id):
    if not is_admin(user_id):
        send_message(chat_id, "❌ Access Denied.")
        return
    pending.pop(user_id, None)
    send_message(
        chat_id,
        "⚙️ <b>ADMIN PANEL</b>\n\n"
        "Manage credentials and up to 9 optional channels.",
        admin_keyboard(user_id),
    )

# ============================================================
# ADMIN CALLBACKS
# ============================================================
def admin_callback(query):
    answer_callback(query["id"])
    user_id = query["from"]["id"]
    message = query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    if chat_id is None or message_id is None:
        return

    if not is_admin(user_id):
        edit_message(chat_id, message_id, "❌ Access Denied.")
        return

    action = query.get("data", "")

    main_only = (
        action in {"set_key", "remove_credentials", "add_public", "add_private"}
        or action.startswith("view_")
        or action.startswith("publicslot")
        or action.startswith("privateslot")
        or action.startswith("refresh_")
        or (action.startswith("remove_") and action not in {"remove_username", "remove_password"})
    )
    if main_only and int(user_id) != MAIN_ADMIN_ID:
        edit_message(chat_id, message_id, "❌ <b>MAIN ADMIN ONLY</b>")
        return

    if action == "admin_back":
        pending.pop(user_id, None)
        edit_message(chat_id, message_id, ("👑 <b>ROCKS MODE — MAIN ADMIN PANEL</b>\n\nSelect an option:" if int(user_id) == MAIN_ADMIN_ID else "⚙️ <b>ADMIN PANEL</b>\n\nSelect an option:"), admin_keyboard(user_id))
        return

    if action in ("set_username", "set_password", "set_key"):
        name = action.replace("set_", "")
        pending[user_id] = {"action": name}
        edit_message(
            chat_id,
            message_id,
            f"📝 <b>{name.upper()} SETTINGS</b>\n\nSend the new {name} now.",
        )
        return

    if action == "remove_username":
        data["username"] = ""
        save_data(data)
        edit_message(chat_id, message_id, "✅ <b>USERNAME REMOVED</b>", admin_keyboard(user_id))
        return

    if action == "remove_password":
        data["password"] = ""
        save_data(data)
        edit_message(chat_id, message_id, "✅ <b>PASSWORD REMOVED</b>", admin_keyboard(user_id))
        return

    if action == "remove_credentials":
        data["username"] = ""
        data["password"] = ""
        data["key"] = ""
        save_data(data)
        edit_message(
            chat_id,
            message_id,
            "✅ <b>CREDENTIALS REMOVED</b>\n\nUsername, password and key were cleared.",
            admin_keyboard(user_id),
        )
        return

    if action == "add_public":
        edit_message(
            chat_id,
            message_id,
            "𝐀𝐃𝐃 𝐏𝐔𝐁𝐋𝐈𝐂 𝐂𝐇𝐀𝐍𝐍𝐄𝐋\n\nChoose a slot:",
            slot_keyboard("publicslot"),
        )
        return

    if action == "add_private":
        edit_message(
            chat_id,
            message_id,
            "𝐀𝐃𝐃 𝐏𝐑𝐈𝐕𝐀𝐓𝐄 𝐂𝐇𝐀𝐍𝐍𝐄𝐋\n\nChoose a slot:",
            slot_keyboard("privateslot"),
        )
        return

    if action.startswith("publicslot_page_") or action.startswith("privateslot_page_"):
        prefix = "publicslot" if action.startswith("publicslot") else "privateslot"
        start = int(action.rsplit("_", 1)[1])
        edit_message(
            chat_id,
            message_id,
            "Choose a channel slot:",
            slot_keyboard(prefix, start),
        )
        return

    if action.startswith("publicslot_") or action.startswith("privateslot_"):
        prefix, raw_index = action.rsplit("_", 1)
        index = int(raw_index)
        ctype = "public" if prefix == "publicslot" else "private"
        pending[user_id] = {"action": "channel", "index": index, "type": ctype}
        example = (
            "@channelusername | https://t.me/channelusername"
            if ctype == "public"
            else "-1001234567890 | https://t.me/+XXXXXXXX"
        )
        edit_message(
            chat_id,
            message_id,
            f"📝 <b>CHANNEL {index + 1} — {ctype.upper()}</b>\n\n"
            "Send one message in this format:\n\n"
            f"<code>{example}</code>\n\n"
            "The first part is the Channel ID and the second part is the Telegram link.",
        )
        return

    if action.startswith("refresh_"):
        index = int(action.rsplit("_", 1)[1])
        if 0 <= index < 9 and data["channels"][index].get("type") == "private":
            channel_id = data["channels"][index].get("id")
            link, error = create_private_invite_link(channel_id)
            if error:
                edit_message(
                    chat_id,
                    message_id,
                    "❌ <b>COULD NOT CREATE NEW PRIVATE LINK</b>\\n\\n"
                    f"{error}\\n\\n"
                    "Make sure the bot is an administrator in this private channel "
                    "with permission to invite users.",
                    channel_info_keyboard(index),
                )
            else:
                data["channels"][index]["link"] = link
                save_data(data)
                edit_message(
                    chat_id,
                    message_id,
                    channel_info(index) + "\\n\\n✅ <b>PRIVATE LINK REFRESHED</b>",
                    channel_info_keyboard(index),
                )
        return

    if action.startswith("view_"):
        index = int(action.rsplit("_", 1)[1]) - 1
        if 0 <= index < 9:
            edit_message(chat_id, message_id, channel_info(index), channel_info_keyboard(index))
        return

    if action.startswith("remove_") and not action.startswith("remove_credentials"):
        index = int(action.rsplit("_", 1)[1])
        if 0 <= index < 9:
            data["channels"][index] = {
                "id": "",
                "link": "",
                "type": DEFAULT_CHANNELS[index]["type"],
            }
            save_data(data)
            edit_message(
                chat_id,
                message_id,
                f"✅ <b>CHANNEL {index + 1} REMOVED</b>\n\n"
                "This slot is now empty and will be ignored by /start.",
                admin_keyboard(user_id),
            )
        return

    if action == "usage":
        configured = configured_channels()
        lines = [
            "📊 <b>BOT USAGE</b>",
            "",
            f"👤 Total Users: <b>{len(data['users'])}</b>",
            f"🔒 Configured Channels: <b>{len(configured)}/9</b>",
            f"👮 Admins: <b>{len(ADMIN_IDS)}/3</b>",
            "",
        ]
        for index, channel in configured:
            lines.append(f"𝐂𝐇𝐀𝐍𝐍𝐄𝐋 {index + 1}: <b>{channel['type'].upper()}</b>")
        lines.extend([
            "",
            f"Username: <b>{'Configured' if data.get('username') else 'Not Configured'}</b>",
            f"Password: <b>{'Configured' if data.get('password') else 'Not Configured'}</b>",
            f"Key: <b>{'Configured' if data.get('key') else 'Not Configured'}</b>",
        ])
        edit_message(chat_id, message_id, "\n".join(lines), admin_keyboard(user_id))
        return

# ============================================================
# ADMIN TEXT INPUT
# ============================================================
def handle_text(chat_id, user_id, text):
    if not is_admin(user_id):
        return

    info = pending.get(user_id)
    if not info:
        return

    text = text.strip()
    if not text:
        send_message(chat_id, "❌ Value cannot be empty.")
        return

    if info["action"] in ("username", "password", "key"):
        name = info["action"]
        data[name] = text
        save_data(data)
        pending.pop(user_id, None)
        send_message(chat_id, f"✅ <b>{name.upper()} UPDATED</b>", admin_keyboard(user_id))
        return

    if info["action"] == "channel":
        try:
            if "|" not in text:
                raise ValueError
            raw_id, link = [x.strip() for x in text.split("|", 1)]
            if not raw_id or not link.startswith("https://t.me/"):
                raise ValueError

            channel_id = int(raw_id) if raw_id.lstrip("-").isdigit() else raw_id
            index = info["index"]
            ctype = info["type"]

            if ctype == "private":
                fresh_link, invite_error = create_private_invite_link(channel_id)
                if invite_error:
                    pending.pop(user_id, None)
                    send_message(
                        chat_id,
                        "❌ <b>PRIVATE CHANNEL NOT ADDED</b>\n\n"
                        f"{invite_error}\n\n"
                        "Add the bot as an administrator in the private channel "
                        "with permission to invite users, then try again.",
                        admin_keyboard(user_id),
                    )
                    return
                link = fresh_link

            data["channels"][index] = {
                "id": channel_id,
                "link": link,
                "type": ctype,
            }
            save_data(data)
            pending.pop(user_id, None)

            msg = (
                f"✅ <b>CHANNEL {index + 1} ADDED</b>\n\n"
                f"Type: <b>{ctype.upper()}</b>\n"
                f"ID: <code>{channel_id}</code>\n"
                f"Link: {link}\n\n"
            )
            msg += (
                "A fresh private invite link was generated automatically."
                if ctype == "private"
                else "Make sure the bot is an administrator in the channel."
            )
            send_message(chat_id, msg, admin_keyboard(user_id))
        except Exception:
            send_message(
                chat_id,
                "❌ <b>INVALID FORMAT</b>\n\n"
                "Use exactly:\n"
                "<code>ID | LINK</code>\n\n"
                "Public example:\n"
                "<code>@channelusername | https://t.me/channelusername</code>\n\n"
                "Private example:\n"
                "<code>-1001234567890 | https://t.me/+XXXXXXXX</code>",
            )

# ============================================================
# UPDATE PROCESSING
# ============================================================
def process_update(update):
    # Private-channel join requests.
    if "chat_join_request" in update:
        request = update["chat_join_request"]
        user = request.get("from", {})
        chat = request.get("chat", {})
        user_id = user.get("id")
        channel_id = chat.get("id")
        if user_id is not None and channel_id is not None:
            join_requests.add((str(channel_id), int(user_id)))
            print(
                f"JOIN REQUEST: user={user_id} channel={channel_id}",
                flush=True,
            )
        return

    if "callback_query" in update:
        query = update["callback_query"]
        action = query.get("data", "")
        if action == "verify":
            verify_callback(query)
        elif (
            action == "admin_back"
            or action == "usage"
            or action.startswith("set_")
            or action.startswith("add_")
            or action.startswith("publicslot")
            or action.startswith("privateslot")
            or action.startswith("view_")
            or action.startswith("remove_")
        ):
            admin_callback(query)
        return

    message = update.get("message")
    if not message:
        return

    user = message.get("from", {})
    user_id = user.get("id")
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")

    if user_id is None or chat_id is None:
        return

    if text.startswith("/start"):
        start(chat_id, user_id)
    elif text.startswith("/admin"):
        admin_panel(chat_id, user_id)
    elif text.startswith("/myid"):
        send_message(chat_id, f"<b>YOUR TELEGRAM ID</b>\n<code>{user_id}</code>")
    elif text and not text.startswith("/"):
        handle_text(chat_id, user_id, text)

# ============================================================
# MAIN
# ============================================================
def main():
    if BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        print("ERROR: BOT_TOKEN is not configured.", flush=True)
        return

    me = api("getMe")
    if not me.get("ok"):
        print("ERROR: Telegram token is invalid or Telegram API is unreachable.", flush=True)
        print(me, flush=True)
        return

    print("==============================================", flush=True)
    print("9-CHANNEL TELEGRAM BOT IS RUNNING", flush=True)
    print("BOT CONNECTED:", me["result"].get("username"), flush=True)
    print("ADMINS:", len(ADMIN_IDS), flush=True)
    print("CONFIGURED CHANNELS:", len(configured_channels()), "/ 9", flush=True)
    print("POLLING Telegram updates...", flush=True)
    print("==============================================", flush=True)

    offset = 0
    while True:
        result = api(
            "getUpdates",
            {
                "offset": offset,
                "timeout": 30,
                "allowed_updates": json.dumps(
                    ["message", "callback_query", "chat_join_request"]
                ),
            },
        )

        if not result.get("ok"):
            time.sleep(3)
            continue

        for update in result.get("result", []):
            offset = update["update_id"] + 1
            try:
                process_update(update)
            except Exception as e:
                print("UPDATE ERROR:", repr(e), flush=True)

if __name__ == "__main__":
    main()
