import os
import logging
import html
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("bubbles-bot")

BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BUBBLES_URL = os.getenv("BUBBLES_URL", "https://example.com").rstrip("/")
WEBHOOK_SECRET = os.environ["BUBBLES_WEBHOOK_SECRET"]  # auths Supabase -> bot (the /bubbles/webhook endpoint)
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")  # auths Telegram -> bot (the /telegram/webhook endpoint); optional but recommended
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # this service's own public URL, e.g. https://bubbles-telegram-bot.onrender.com — if set, the webhook registers itself on startup

http = httpx.AsyncClient(timeout=15.0)
tg_app: Application | None = None

# Real event types this codebase actually generates — see the check
# constraint on public.bubbles_notifications in supabase.sql — plus a
# synthetic "message" pseudo-type for rows from public.messages
# (which has no "type" column at all, it's implicitly one kind of
# event). Anything not in this dict still gets a generic label instead
# of failing, so a future notification type added to Bubbles is
# covered automatically.
NOTIF_LABELS: dict[str, tuple[str, str]] = {
    "friend_request": ("👥", "Новая заявка в друзья"),
    "friend_accept": ("🤝", "Заявка в друзья принята"),
    "post_like": ("❤️", "Новый лайк"),
    "post_comment": ("💬", "Новый комментарий"),
    "comment_reply": ("💬", "Ответ на комментарий"),
    "comment_like": ("❤️", "Лайк на комментарий"),
    "wall_post": ("🧱", "Запись на стене"),
    "pet_fed": ("🍬", "Питомец покормлен"),
}
MESSAGE_LABEL = ("💬", "Новое сообщение")

# Maps a notification TYPE to the notification_settings COLUMN that
# gates it. friend_request/friend_accept -> friends_enabled, the two
# like types and two comment types are grouped the same way the
# /settings toggles group them. wall_post and pet_fed are deliberately
# left unmapped: with no matching toggle they're always delivered
# rather than silently dropped.
TYPE_TO_COLUMN = {
    "post_like": "likes_enabled",
    "comment_like": "likes_enabled",
    "post_comment": "comments_enabled",
    "comment_reply": "comments_enabled",
    "friend_request": "friends_enabled",
    "friend_accept": "friends_enabled",
}
CB_TO_COLUMN = {"toggle_message": "messages_enabled", "toggle_comment": "comments_enabled", "toggle_like": "likes_enabled", "toggle_friend": "friends_enabled"}


def headers() -> dict[str, str]:
    return {"apikey": SUPABASE_SERVICE_ROLE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}", "Content-Type": "application/json", "Prefer": "return=representation"}


async def sb(method: str, path: str, *, params: dict[str, Any] | None = None, json_data: Any | None = None) -> Any:
    r = await http.request(method, f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}", headers=headers(), params=params, json=json_data)
    if r.status_code >= 400:
        log.error("Supabase %s %s -> %s: %s", method, path, r.status_code, r.text[:1000])
        raise RuntimeError(f"Supabase HTTP {r.status_code}")
    return r.json() if r.content else None


async def link_by_chat(chat_id: int) -> dict[str, Any] | None:
    rows = await sb("GET", "telegram_links", params={"telegram_chat_id": f"eq.{chat_id}", "limit": "1"})
    return rows[0] if rows else None


async def settings_for(user_id: str) -> dict[str, Any]:
    rows = await sb("GET", "notification_settings", params={"user_id": f"eq.{user_id}", "limit": "1"})
    if rows:
        return rows[0]
    rows = await sb("POST", "notification_settings", json_data={"user_id": user_id, "messages_enabled": True, "comments_enabled": True, "likes_enabled": True, "friends_enabled": True})
    return rows[0]


async def actor_display_name(actor_id: str | None) -> str:
    if not actor_id:
        return "Кто-то"
    rows = await sb("GET", "profiles", params={"id": f"eq.{actor_id}", "select": "display_name,username", "limit": "1"})
    if not rows:
        return "Кто-то"
    p = rows[0]
    return p.get("display_name") or p.get("username") or "Кто-то"


def settings_keyboard(s: dict[str, Any]) -> InlineKeyboardMarkup:
    def m(key: str, name: str, cb: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(("✅" if s.get(key) else "❌") + " " + name, callback_data=cb)
    return InlineKeyboardMarkup([
        [m("messages_enabled", "Сообщения", "toggle_message")],
        [m("comments_enabled", "Комментарии", "toggle_comment")],
        [m("likes_enabled", "Лайки", "toggle_like")],
        [m("friends_enabled", "Друзья", "toggle_friend")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="settings_refresh")],
    ])


async def show_settings(chat_id: int, edit=None) -> None:
    link = await link_by_chat(chat_id)
    if not link:
        text = "⚪ <b>Telegram не подключён</b>\n\nОткрой Bubbles → Настройки профиля → «Подключить Telegram»."
        if edit: await edit.edit_text(text, parse_mode=ParseMode.HTML)
        else: await tg_app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        return
    s = await settings_for(link["user_id"])
    text = ("⚙️ <b>Уведомления Bubbles</b>\n\n"
            f"💬 Сообщения: {'включены' if s['messages_enabled'] else 'выключены'}\n"
            f"💬 Комментарии: {'включены' if s['comments_enabled'] else 'выключены'}\n"
            f"❤️ Лайки: {'включены' if s['likes_enabled'] else 'выключены'}\n"
            f"👥 Друзья: {'включены' if s['friends_enabled'] else 'выключены'}\n\n"
            "🧱 Записи на стене и 🍬 кормление питомца приходят всегда.")
    if edit: await edit.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=settings_keyboard(s))
    else: await tg_app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=settings_keyboard(s))


LINKED_KEYBOARD = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Настройки уведомлений", callback_data="settings_open")], [InlineKeyboardButton("🫧 Открыть Bubbles", url=BUBBLES_URL)]])


# Shared by both /start CODE (deep link — what the "✈️ Открыть бота"
# button in Bubbles' settings page opens) and /link CODE (manual entry
# — what the copy-paste instructions in Bubbles' settings page ask
# for). Both end up doing exactly the same thing to exactly the same
# tables, they just get the code from different places.
async def consume_link_token(raw_token: str, chat_id: int, username: str | None) -> tuple[bool, str]:
    token = (raw_token or "").strip().upper()
    if not token:
        return False, "Пришлите код из настроек Bubbles вот так:\n<code>/link BUB-XXXXXX</code>"

    rows = await sb("GET", "telegram_link_tokens", params={"token": f"eq.{token}", "limit": "1"})
    if not rows:
        return False, "❌ Такой код не найден. Проверьте, что ввели его без опечаток, или сгенерируйте новый в настройках Bubbles."

    row = rows[0]
    expires_at = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    if expires_at < datetime.now(timezone.utc):
        await sb("DELETE", "telegram_link_tokens", params={"token": f"eq.{token}"})
        return False, "⌛ Код истёк — коды действуют 10 минут. Сгенерируйте новый в настройках Bubbles."

    user_id = row["user_id"]

    # This Telegram chat might already be linked to a DIFFERENT
    # Bubbles account — unlink it there first so a chat never ends up
    # pointing at two accounts at once.
    await sb("DELETE", "telegram_links", params={"telegram_chat_id": f"eq.{chat_id}"})
    # This Bubbles account might already point at a different chat
    # (re-linking) — replace it instead of ending up with two rows.
    await sb("DELETE", "telegram_links", params={"user_id": f"eq.{user_id}"})

    await sb("POST", "telegram_links", json_data={"user_id": user_id, "telegram_chat_id": chat_id, "telegram_username": username, "enabled": True})
    # Single-use: the code is spent now, whether or not it was this
    # exact row (deleting by token also covers the already-expired
    # case above, so nothing is ever left behind to be reused).
    await sb("DELETE", "telegram_link_tokens", params={"token": f"eq.{token}"})
    await settings_for(user_id)  # make sure a settings row exists so /settings works immediately

    return True, "✅ <b>Telegram успешно подключён к вашему аккаунту Bubbles!</b>\n\nНастройте, какие уведомления присылать, через /settings."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user: return
    chat_id = update.effective_chat.id
    token = context.args[0] if context.args else None
    if token:
        ok, text = await consume_link_token(token, chat_id, update.effective_user.username)
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=LINKED_KEYBOARD if ok else None)
        return
    if await link_by_chat(chat_id):
        await update.effective_message.reply_text("🫧 <b>Bubbles Notifications</b>\n\nTelegram уже подключён.\n\n/settings — настройки\n/status — статус\n/unlink — отключить", parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text("🫧 <b>Bubbles Notifications</b>\n\nОткрой Bubbles → Настройки профиля → «Подключить Telegram», получи код и пришли его сюда:\n<code>/link BUB-XXXXXX</code>", parse_mode=ParseMode.HTML)


async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user: return
    arg = context.args[0] if context.args else ""
    ok, text = await consume_link_token(arg, update.effective_chat.id, update.effective_user.username)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=LINKED_KEYBOARD if ok else None)


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat: await show_settings(update.effective_chat.id)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat: return
    link = await link_by_chat(update.effective_chat.id)
    await update.effective_message.reply_text("🟢 <b>Подключение активно</b>" if link else "⚪ Telegram сейчас не подключён.", parse_mode=ParseMode.HTML)


async def unlink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat: return
    existing = await link_by_chat(update.effective_chat.id)
    if not existing:
        await update.effective_message.reply_text("Этот Telegram и так не подключён ни к одному аккаунту Bubbles.")
        return
    await sb("DELETE", "telegram_links", params={"telegram_chat_id": f"eq.{update.effective_chat.id}"})
    await update.effective_message.reply_text("🔕 Telegram отключён от Bubbles.")


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not update.effective_chat: return
    await q.answer()
    if q.data in ("settings_open", "settings_refresh"):
        await show_settings(update.effective_chat.id, edit=q.message)
        return
    col = CB_TO_COLUMN.get(q.data)
    if not col: return
    link = await link_by_chat(update.effective_chat.id)
    if not link:
        await q.message.edit_text("⚪ Telegram не подключён.")
        return
    s = await settings_for(link["user_id"])
    await sb("PATCH", "notification_settings", params={"user_id": f"eq.{link['user_id']}"}, json_data={col: not bool(s.get(col)), "updated_at": datetime.now(timezone.utc).isoformat()})
    await show_settings(update.effective_chat.id, edit=q.message)


def notification_text_for_bubbles_notification(kind: str, name: str) -> str:
    icon, title = NOTIF_LABELS.get(kind, ("🔔", "Уведомление Bubbles"))
    name_esc = html.escape(name)
    body = {
        "friend_request": f"<b>{name_esc}</b> отправил(а) вам заявку в друзья.",
        "friend_accept": f"<b>{name_esc}</b> принял(а) вашу заявку в друзья.",
        "post_like": f"<b>{name_esc}</b> оценил(а) ваш пост.",
        "post_comment": f"<b>{name_esc}</b> прокомментировал(а) ваш пост.",
        "comment_reply": f"<b>{name_esc}</b> ответил(а) на ваш комментарий.",
        "comment_like": f"<b>{name_esc}</b> оценил(а) ваш комментарий.",
        "wall_post": f"<b>{name_esc}</b> оставил(а) запись на вашей стене.",
        "pet_fed": f"<b>{name_esc}</b> покормил(а) вашего питомца.",
    }.get(kind, f"<b>{name_esc}</b>: новое уведомление в Bubbles.")
    return f"{icon} <b>{html.escape(title)}</b>\n\n{body}"


def notification_text_for_message(name: str) -> str:
    icon, title = MESSAGE_LABEL
    # Deliberately NOT including the message text: messages.text can be
    # end-to-end encrypted client-side (see the `encrypted` column on
    # public.messages in supabase.sql), and even when it isn't, a
    # Telegram notification is the wrong place for it regardless —
    # same reasoning supabase/functions/send-push/ already uses for
    # browser push. The content only ever gets decrypted client-side.
    return f"{icon} <b>{html.escape(title)}</b>\n\n<b>{html.escape(name)}</b> написал(а) вам сообщение."


async def deliver(chat_id: int, text: str) -> None:
    try:
        await tg_app.bot.send_message(
            chat_id, text, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🫧 Открыть Bubbles", url=BUBBLES_URL)]]),
            disable_web_page_preview=True,
        )
    except Forbidden:
        # Person blocked the bot or deleted their Telegram account —
        # clean up so future events don't keep trying a dead chat.
        await sb("DELETE", "telegram_links", params={"telegram_chat_id": f"eq.{chat_id}"})


async def process_bubbles_notification(record: dict[str, Any]) -> None:
    row_id, user_id, kind = record.get("id"), record.get("user_id"), record.get("type")
    if not row_id or not user_id or not kind:
        return
    # Re-fetch the row fresh rather than trusting the webhook payload —
    # Supabase Database Webhooks retry on a non-2xx response, and this
    # is what stops a retry (or an outage recovery) from sending the
    # same notification twice.
    fresh = await sb("GET", "bubbles_notifications", params={"id": f"eq.{row_id}", "select": "id,user_id,actor_id,type,telegram_sent_at", "limit": "1"})
    if not fresh or fresh[0].get("telegram_sent_at"):
        return
    links = await sb("GET", "telegram_links", params={"user_id": f"eq.{user_id}", "enabled": "eq.true", "limit": "1"})
    if not links:
        return  # no Telegram linked (or muted) — nothing to do, not an error
    settings = await settings_for(user_id)
    col = TYPE_TO_COLUMN.get(kind)
    if col and not settings.get(col, True):
        return
    name = await actor_display_name(fresh[0].get("actor_id"))
    await deliver(int(links[0]["telegram_chat_id"]), notification_text_for_bubbles_notification(kind, name))
    await sb("PATCH", "bubbles_notifications", params={"id": f"eq.{row_id}"}, json_data={"telegram_sent_at": datetime.now(timezone.utc).isoformat()})


async def process_message(record: dict[str, Any]) -> None:
    row_id, receiver_id, sender_id = record.get("id"), record.get("receiver_id"), record.get("sender_id")
    if not row_id or not receiver_id:
        return
    fresh = await sb("GET", "messages", params={"id": f"eq.{row_id}", "select": "id,sender_id,receiver_id,telegram_sent_at", "limit": "1"})
    if not fresh or fresh[0].get("telegram_sent_at"):
        return
    links = await sb("GET", "telegram_links", params={"user_id": f"eq.{receiver_id}", "enabled": "eq.true", "limit": "1"})
    if not links:
        return
    settings = await settings_for(receiver_id)
    if not settings.get("messages_enabled", True):
        return
    name = await actor_display_name(sender_id)
    await deliver(int(links[0]["telegram_chat_id"]), notification_text_for_message(name))
    await sb("PATCH", "messages", params={"id": f"eq.{row_id}"}, json_data={"telegram_sent_at": datetime.now(timezone.utc).isoformat()})


app = FastAPI(title="Bubbles Telegram Notifications")


@app.get("/health")
async def health(): return {"ok": True, "service": "bubbles-telegram-notifications"}


@app.post("/bubbles/webhook")
async def bubbles_webhook(request: Request, x_bubbles_webhook_secret: str | None = Header(default=None)):
    if x_bubbles_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid webhook secret")
    payload = await request.json()
    if payload.get("type") != "INSERT":
        return JSONResponse({"ok": True, "ignored": True})
    table = payload.get("table")
    record = payload.get("record") or {}
    try:
        if table == "bubbles_notifications":
            await process_bubbles_notification(record)
        elif table == "messages":
            await process_message(record)
        else:
            return JSONResponse({"ok": True, "ignored": True})
    except Exception:
        log.exception("Notification processing failed")
        raise HTTPException(status_code=500, detail="notification processing failed")
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if TELEGRAM_WEBHOOK_SECRET and x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid telegram webhook secret")
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tg_app
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("link", link_cmd))
    tg_app.add_handler(CommandHandler("settings", settings_cmd))
    tg_app.add_handler(CommandHandler("status", status_cmd))
    tg_app.add_handler(CommandHandler("unlink", unlink_cmd))
    tg_app.add_handler(CallbackQueryHandler(callbacks))
    await tg_app.initialize()
    await tg_app.start()
    # Webhook mode, not long polling: a free-tier host (Render, etc.)
    # spins the service down after a stretch with no INCOMING HTTP
    # requests. Long polling is the bot making OUTGOING requests to
    # Telegram in a loop — invisible to that mechanism — so the host
    # would happily kill the process mid-poll ("бот уснул"). Webhook
    # mode means Telegram makes the incoming request instead, which
    # both counts as traffic and wakes a sleeping instance on demand.
    if PUBLIC_URL:
        await tg_app.bot.set_webhook(
            url=f"{PUBLIC_URL}/telegram/webhook",
            secret_token=TELEGRAM_WEBHOOK_SECRET or None,
            drop_pending_updates=True,
        )
        log.info("Telegram webhook registered at %s/telegram/webhook", PUBLIC_URL)
    else:
        log.warning("PUBLIC_URL not set — webhook was NOT auto-registered. Register it manually (see README.md) or the bot won't receive any messages.")
    yield
    await tg_app.stop()
    await tg_app.shutdown()
    await http.aclose()


app.router.lifespan_context = lifespan
