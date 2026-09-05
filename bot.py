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
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("bubbles-bot")

BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BUBBLES_URL = os.getenv("BUBBLES_URL", "https://example.com").rstrip("/")
WEBHOOK_SECRET = os.environ["BUBBLES_WEBHOOK_SECRET"]

http = httpx.AsyncClient(timeout=15.0)
tg_app: Application | None = None

LABELS = {
    "message": ("💬", "Новое сообщение"),
    "comment": ("💭", "Новый комментарий"),
    "like": ("❤️", "Новая реакция"),
    "friend_request": ("👥", "Новая заявка в друзья"),
    "friend_accept": ("🤝", "Заявка в друзья принята"),
    "mention": ("✨", "Вас упомянули"),
    "system": ("🔔", "Уведомление Bubbles"),
}
CB_TO_TYPE = {"toggle_message": "message", "toggle_comment": "comment", "toggle_like": "like", "toggle_friend": "friend_request"}
TYPE_TO_COLUMN = {"message": "messages_enabled", "comment": "comments_enabled", "like": "likes_enabled", "friend_request": "friends_enabled", "friend_accept": "friends_enabled"}


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


def settings_keyboard(s: dict[str, Any]) -> InlineKeyboardMarkup:
    m = lambda key, name: InlineKeyboardButton(("✅" if s[key] else "❌") + " " + name, callback_data=key.replace("_enabled", "").replace("messages", "toggle_message").replace("comments", "toggle_comment").replace("likes", "toggle_like").replace("friends", "toggle_friend"))
    return InlineKeyboardMarkup([
        [m("messages_enabled", "Сообщения")],
        [m("comments_enabled", "Комментарии")],
        [m("likes_enabled", "Лайки")],
        [m("friends_enabled", "Друзья")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="settings_refresh")],
    ])


async def show_settings(chat_id: int, edit=None) -> None:
    link = await link_by_chat(chat_id)
    if not link:
        text = "⚪ <b>Telegram не подключён</b>\n\nОткрой Bubbles → настройки → «Подключить Telegram»."
        if edit: await edit.edit_text(text, parse_mode=ParseMode.HTML)
        else: await tg_app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        return
    s = await settings_for(link["user_id"])
    text = ("⚙️ <b>Уведомления Bubbles</b>\n\n"
            f"💬 Сообщения: {'включены' if s['messages_enabled'] else 'выключены'}\n"
            f"💭 Комментарии: {'включены' if s['comments_enabled'] else 'выключены'}\n"
            f"❤️ Лайки: {'включены' if s['likes_enabled'] else 'выключены'}\n"
            f"👥 Друзья: {'включены' if s['friends_enabled'] else 'выключены'}")
    if edit: await edit.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=settings_keyboard(s))
    else: await tg_app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=settings_keyboard(s))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user: return
    chat_id = update.effective_chat.id
    token = context.args[0] if context.args else None
    if token:
        rows = await sb("GET", "telegram_link_tokens", params={"token": f"eq.{token}", "expires_at": f"gt.{datetime.now(timezone.utc).isoformat()}", "limit": "1"})
        if not rows:
            await update.effective_message.reply_text("❌ Ссылка подключения недействительна или истекла.")
            return
        user_id = rows[0]["user_id"]
        await sb("DELETE", "telegram_links", params={"telegram_chat_id": f"eq.{chat_id}"})
        await sb("POST", "telegram_links", json_data={"user_id": user_id, "telegram_chat_id": chat_id, "telegram_username": update.effective_user.username, "enabled": True})
        await sb("DELETE", "telegram_link_tokens", params={"token": f"eq.{token}"})
        await settings_for(user_id)
        await update.effective_message.reply_text("✅ <b>Bubbles подключён!</b>\n\nТеперь я буду присылать уведомления в Telegram.\n\n/settings — настройки", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Настройки", callback_data="settings_open")], [InlineKeyboardButton("🫧 Открыть Bubbles", url=BUBBLES_URL)]]))
        return
    if await link_by_chat(chat_id):
        await update.effective_message.reply_text("🫧 <b>Bubbles Notifications</b>\n\nTelegram уже подключён.\n\n/settings — настройки\n/status — статус\n/unlink — отключить", parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text("🫧 <b>Bubbles Notifications</b>\n\nОткрой Bubbles → настройки профиля → «Подключить Telegram».", parse_mode=ParseMode.HTML)


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat: await show_settings(update.effective_chat.id)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat: return
    link = await link_by_chat(update.effective_chat.id)
    await update.effective_message.reply_text("🟢 <b>Подключение активно</b>" if link else "⚪ Telegram сейчас не подключён.", parse_mode=ParseMode.HTML)


async def unlink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat: return
    await sb("DELETE", "telegram_links", params={"telegram_chat_id": f"eq.{update.effective_chat.id}"})
    await update.effective_message.reply_text("🔕 Telegram отключён от Bubbles.")


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not update.effective_chat: return
    await q.answer()
    if q.data in ("settings_open", "settings_refresh"):
        await show_settings(update.effective_chat.id, edit=q.message)
        return
    event = CB_TO_TYPE.get(q.data)
    if not event: return
    link = await link_by_chat(update.effective_chat.id)
    if not link:
        await q.message.edit_text("⚪ Telegram не подключён.")
        return
    col = TYPE_TO_COLUMN[event]
    s = await settings_for(link["user_id"])
    await sb("PATCH", "notification_settings", params={"user_id": f"eq.{link['user_id']}"}, json_data={col: not bool(s[col]), "updated_at": datetime.now(timezone.utc).isoformat()})
    await show_settings(update.effective_chat.id, edit=q.message)


def notification_text(rec: dict[str, Any]) -> str:
    kind = rec.get("type", "system")
    icon, title = LABELS.get(kind, LABELS["system"])
    actor = html.escape(str(rec.get("actor_name") or "Кто-то"))
    body = html.escape(str(rec.get("content") or ""))[:700]
    if kind == "message": suffix = f"<b>{actor}</b> написал(а) тебе" + (f":\n«{body}»" if body else "")
    elif kind == "comment": suffix = f"<b>{actor}</b> оставил(а) комментарий" + (f":\n«{body}»" if body else "")
    elif kind == "like": suffix = f"<b>{actor}</b> поставил(а) реакцию на твою публикацию."
    elif kind == "friend_request": suffix = f"<b>{actor}</b> отправил(а) тебе заявку в друзья."
    elif kind == "friend_accept": suffix = f"<b>{actor}</b> принял(а) твою заявку в друзья."
    elif kind == "mention": suffix = f"<b>{actor}</b> упомянул(а) тебя." + (f"\n\n«{body}»" if body else "")
    else: suffix = body or "Появилось новое событие в Bubbles."
    return f"{icon} <b>{html.escape(title)}</b>\n\n{suffix}"


async def process_notification(rec: dict[str, Any]) -> None:
    nid, uid, kind = rec.get("id"), rec.get("user_id"), rec.get("type", "system")
    if not nid or not uid: return
    existing = await sb("GET", "notifications", params={"id": f"eq.{nid}", "limit": "1"})
    if not existing or existing[0].get("telegram_sent_at"): return
    links = await sb("GET", "telegram_links", params={"user_id": f"eq.{uid}", "enabled": "eq.true", "limit": "1"})
    if not links: return
    s = await settings_for(uid)
    col = TYPE_TO_COLUMN.get(kind)
    if col and not s.get(col, True): return
    chat = int(links[0]["telegram_chat_id"])
    await tg_app.bot.send_message(chat, notification_text(rec), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🫧 Открыть Bubbles", url=BUBBLES_URL)]]), disable_web_page_preview=True)
    await sb("PATCH", "notifications", params={"id": f"eq.{nid}"}, json_data={"telegram_sent_at": datetime.now(timezone.utc).isoformat()})


app = FastAPI(title="Bubbles Telegram Notifications")


@app.get("/health")
async def health(): return {"ok": True, "service": "bubbles-telegram-notifications"}


@app.post("/bubbles/webhook")
async def bubbles_webhook(request: Request, x_bubbles_webhook_secret: str | None = Header(default=None)):
    if x_bubbles_webhook_secret != WEBHOOK_SECRET: raise HTTPException(status_code=401, detail="invalid webhook secret")
    payload = await request.json()
    if payload.get("type") != "INSERT" or payload.get("table") != "notifications": return JSONResponse({"ok": True, "ignored": True})
    try:
        await process_notification(payload.get("record") or {})
    except Exception:
        log.exception("Notification processing failed")
        raise HTTPException(status_code=500, detail="notification processing failed")
    return {"ok": True}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tg_app
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("settings", settings_cmd))
    tg_app.add_handler(CommandHandler("status", status_cmd))
    tg_app.add_handler(CommandHandler("unlink", unlink_cmd))
    tg_app.add_handler(CallbackQueryHandler(callbacks))
    await tg_app.initialize(); await tg_app.start(); await tg_app.updater.start_polling(drop_pending_updates=True)
    yield
    await tg_app.updater.stop(); await tg_app.stop(); await tg_app.shutdown(); await http.aclose()
    app.router.lifespan_context = lifespan
