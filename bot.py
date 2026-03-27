"""
bot.py — Telegram-бот для автоматической регистрации аккаунтов Kleinanzeigen.

Команды:
  /start   — приветствие
  /reg     — начать пошаговую регистрацию
  /cancel  — отменить текущий процесс
  /help    — справка
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN, ADMIN_IDS, EMAIL_CHECK_TIMEOUT
from email_helper import async_fetch_verification_link
from registrar import KleinanzeigenRegistrar, generate_password

# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
(
    WAITING_PROXY,
    WAITING_EMAIL,
    WAITING_EMAIL_PASS,
    WAITING_PHONE,
    WAITING_SMS,
) = range(5)

# ── Per-user state storage (хранится в context.user_data) ────────────────────
KEY_PROXY       = "proxy"
KEY_EMAIL       = "email"
KEY_EMAIL_PASS  = "email_pass"
KEY_PHONE       = "phone"
KEY_PASSWORD    = "ka_password"   # сгенерированный пароль для KA
KEY_REGISTRAR   = "registrar"     # объект KleinanzeigenRegistrar


# ─────────────────────────────────────────────────────────────────────────────
# Auth decorator
# ─────────────────────────────────────────────────────────────────────────────

def admin_only(func):
    """Разрешает вызов только администраторам из ADMIN_IDS."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if ADMIN_IDS and uid not in ADMIN_IDS:
            await update.effective_message.reply_text(
                "⛔ У вас нет доступа к этому боту."
            )
            return ConversationHandler.END
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _cleanup_registrar(ctx: ContextTypes.DEFAULT_TYPE):
    reg: Optional[KleinanzeigenRegistrar] = ctx.user_data.pop(KEY_REGISTRAR, None)
    if reg:
        await reg._stop()


async def _send(update: Update, text: str, **kwargs):
    """Отправляет ответ в зависимости от типа апдейта."""
    msg = update.effective_message
    if msg:
        await msg.reply_text(text, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# /start, /help
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>Kleinanzeigen Auto-Reg Bot</b>\n\n"
        "Бот автоматически регистрирует аккаунт на Kleinanzeigen.de.\n\n"
        "Команды:\n"
        "/reg — начать регистрацию\n"
        "/cancel — отменить\n"
        "/help — справка"
    )
    await _send(update, text, parse_mode=ParseMode.HTML)


@admin_only
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>Справка</b>\n\n"
        "<b>Формат прокси:</b>\n"
        "<code>ip:port</code> — без авторизации\n"
        "<code>ip:port:login:password</code> — с авторизацией\n"
        "Введите <code>-</code> если прокси не нужен.\n\n"
        "<b>Email-пароль</b> нужен для автоматического считывания письма верификации через IMAP.\n\n"
        "<b>Телефон</b> вводится в международном формате: <code>+491234567890</code>\n\n"
        "После отправки номера телефона дождитесь SMS и введите код."
    )
    await _send(update, text, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────────────────────────────────────────
# /cancel
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _cleanup_registrar(ctx)
    ctx.user_data.clear()
    await _send(update, "🚫 Регистрация отменена.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — /reg → ask proxy
# ─────────────────────────────────────────────────────────────────────────────

@admin_only
async def cmd_reg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await _send(
        update,
        "🔄 <b>Шаг 1/4</b> — Введите прокси.\n\n"
        "Форматы:\n"
        "<code>ip:port</code>\n"
        "<code>ip:port:login:password</code>\n\n"
        "Введите <code>-</code> если прокси не нужен.",
        parse_mode=ParseMode.HTML,
    )
    return WAITING_PROXY


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — receive proxy → ask email
# ─────────────────────────────────────────────────────────────────────────────

async def recv_proxy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    ctx.user_data[KEY_PROXY] = None if raw in ("-", "нет", "no") else raw

    await _send(
        update,
        "📧 <b>Шаг 2/4</b> — Введите email для регистрации на Kleinanzeigen:",
        parse_mode=ParseMode.HTML,
    )
    return WAITING_EMAIL


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — receive email → ask email password (for IMAP)
# ─────────────────────────────────────────────────────────────────────────────

async def recv_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    email = (update.message.text or "").strip()
    if "@" not in email:
        await _send(update, "❌ Некорректный email. Попробуйте снова:")
        return WAITING_EMAIL

    ctx.user_data[KEY_EMAIL] = email
    await _send(
        update,
        "🔑 <b>Шаг 3/4</b> — Введите пароль от этого email-ящика.\n"
        "<i>Нужен для автоматической проверки письма верификации через IMAP.</i>\n\n"
        "⚠️ Пароль используется только локально и никуда не передаётся.",
        parse_mode=ParseMode.HTML,
    )
    return WAITING_EMAIL_PASS


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — receive email pass → ask phone
# ─────────────────────────────────────────────────────────────────────────────

async def recv_email_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data[KEY_EMAIL_PASS] = (update.message.text or "").strip()
    await _send(
        update,
        "📱 <b>Шаг 4/4</b> — Введите номер телефона.\n"
        "Формат: <code>+491234567890</code>\n\n"
        "Введите <code>-</code> чтобы пропустить шаг с телефоном.",
        parse_mode=ParseMode.HTML,
    )
    return WAITING_PHONE


# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — receive phone → start registration
# ─────────────────────────────────────────────────────────────────────────────

async def recv_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone_raw = (update.message.text or "").strip()
    ctx.user_data[KEY_PHONE] = None if phone_raw in ("-", "нет", "no") else phone_raw

    email      = ctx.user_data[KEY_EMAIL]
    email_pass = ctx.user_data[KEY_EMAIL_PASS]
    proxy      = ctx.user_data.get(KEY_PROXY)
    phone      = ctx.user_data.get(KEY_PHONE)
    ka_password = generate_password()
    ctx.user_data[KEY_PASSWORD] = ka_password

    await _send(
        update,
        f"🚀 Начинаю регистрацию…\n\n"
        f"📧 Email: <code>{_escape(email)}</code>\n"
        f"🔐 Пароль KA: <code>{_escape(ka_password)}</code>\n"
        f"🌐 Прокси: <code>{_escape(proxy or 'нет')}</code>\n"
        f"📱 Телефон: <code>{_escape(phone or 'не указан')}</code>",
        parse_mode=ParseMode.HTML,
    )

    # Run registration in background task so we can stay reactive
    ctx.application.create_task(
        _run_registration(update, ctx, email, email_pass, ka_password, proxy, phone),
        update=update,
    )
    return WAITING_SMS


# ─────────────────────────────────────────────────────────────────────────────
# Background: full registration flow
# ─────────────────────────────────────────────────────────────────────────────

async def _run_registration(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    email: str,
    email_pass: str,
    ka_password: str,
    proxy: Optional[str],
    phone: Optional[str],
):
    chat_id = update.effective_chat.id
    bot = ctx.application.bot

    async def notify(text: str):
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    async def send_logs(reg: KleinanzeigenRegistrar, last_n: int = 15, label: str = "Лог"):
        """Отправить последние N строк логов из регистратора."""
        chunk = reg.logs[-last_n:] if len(reg.logs) > last_n else reg.logs
        if chunk:
            text = f"<b>{_escape(label)}</b>\n<pre>{_escape(chr(10).join(chunk))}</pre>"
            # Telegram limit 4096 chars — truncate if needed
            if len(text) > 4000:
                text = text[:3990] + "\n…</pre>"
            try:
                await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
            except Exception:
                pass

    reg = KleinanzeigenRegistrar(proxy_str=proxy)
    ctx.user_data[KEY_REGISTRAR] = reg

    try:
        await reg._start()
        await send_logs(reg, label="🚀 Браузер запущен")

        # ── 1. Fill registration form ─────────────────────────────────────────
        await notify("⚙️ Заполняю форму регистрации…")
        ok = await reg.fill_registration_form(email, ka_password)
        await send_logs(reg, last_n=20, label="📋 Лог формы регистрации")

        if not ok:
            await notify("❌ Не удалось заполнить форму регистрации.")
            return

        # ── 2. Email verification ──────────────────────────────────────────────
        await notify(
            f"📬 Жду письмо верификации на <code>{_escape(email)}</code>…\n"
            f"<i>Таймаут: {EMAIL_CHECK_TIMEOUT} сек.</i>"
        )
        link = await async_fetch_verification_link(
            email, email_pass, timeout=EMAIL_CHECK_TIMEOUT
        )

        if not link:
            await notify(
                "⚠️ Письмо верификации не пришло за отведённое время.\n"
                "Проверьте ящик вручную или увеличьте <code>EMAIL_CHECK_TIMEOUT</code>."
            )
            # Don't abort — maybe phone step is needed
        else:
            await notify("✅ Письмо найдено. Открываю ссылку верификации…")
            await reg.open_verification_link(link)
            await send_logs(reg, last_n=10, label="📋 Лог верификации email")
            await notify("✅ Email подтверждён!")

        # ── 3. Phone step ─────────────────────────────────────────────────────
        if phone:
            await notify(f"📱 Добавляю номер <code>{_escape(phone)}</code>…")
            phone_ok = await reg.enter_phone_number(phone)
            await send_logs(reg, last_n=10, label="📋 Лог телефона")
            if phone_ok:
                await notify(
                    "📨 SMS-код отправлен!\n"
                    "Введите код из SMS:"
                )
                # WAITING_SMS state is already active — wait for user input
                ctx.user_data["_sms_waiting"] = True
                # The bot will call recv_sms when user sends the code
                return  # Don't close registrar yet
            else:
                await notify("⚠️ Шаг с телефоном не обнаружен на странице — пропускаю.")

        # ── 4. Done ───────────────────────────────────────────────────────────
        await _finish(bot, chat_id, ctx)

    except Exception as exc:
        logger.exception("Registration error")
        await send_logs(reg, last_n=25, label="📋 Полный лог ошибки")
        await notify(f"❌ Ошибка регистрации: <code>{_escape(str(exc))}</code>")
        await _cleanup_registrar(ctx)


async def _finish(bot, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    email      = ctx.user_data.get(KEY_EMAIL, "?")
    ka_password = ctx.user_data.get(KEY_PASSWORD, "?")
    text = (
        "🎉 <b>Регистрация завершена!</b>\n\n"
        f'🌐 Сайт: <a href="https://www.kleinanzeigen.de">kleinanzeigen.de</a>\n'
        f"📧 Email: <code>{_escape(email)}</code>\n"
        f"🔐 Пароль: <code>{_escape(ka_password)}</code>\n\n"
        "<i>Сохраните данные — бот их больше не хранит.</i>"
    )
    await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    await _cleanup_registrar(ctx)
    ctx.user_data.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Step 6 — receive SMS code
# ─────────────────────────────────────────────────────────────────────────────

async def recv_sms(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("_sms_waiting"):
        await _send(update, "❓ Нет активного запроса SMS-кода.")
        return ConversationHandler.END

    code = (update.message.text or "").strip()
    reg: Optional[KleinanzeigenRegistrar] = ctx.user_data.get(KEY_REGISTRAR)
    if not reg:
        await _send(update, "❌ Сессия регистрации не найдена. Начните заново /reg")
        return ConversationHandler.END

    await _send(update, f"🔢 Ввожу код <code>{_escape(code)}</code>…", parse_mode=ParseMode.HTML)
    sms_ok = await reg.enter_sms_code(code)

    if sms_ok:
        await _send(update, "✅ Телефон подтверждён!", parse_mode=ParseMode.HTML)
    else:
        await _send(
            update,
            "⚠️ Не удалось ввести код автоматически. Проверьте аккаунт вручную.",
            parse_mode=ParseMode.HTML,
        )

    ctx.user_data.pop("_sms_waiting", None)
    await _finish(ctx.application.bot, update.effective_chat.id, ctx)
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────

def _escape(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ─────────────────────────────────────────────────────────────────────────────
# Application setup
# ─────────────────────────────────────────────────────────────────────────────

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("reg", cmd_reg)],
        states={
            WAITING_PROXY:      [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_proxy)],
            WAITING_EMAIL:      [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_email)],
            WAITING_EMAIL_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_email_pass)],
            WAITING_PHONE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_phone)],
            WAITING_SMS:        [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_sms)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(conv)

    return app


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан. Заполните файл .env")
        return

    logger.info("Бот запущен. Жду сообщений…")
    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
