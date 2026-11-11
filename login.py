"""Бот-уведомитель о сообщениях в Telegram.

Два клиента в одном процессе:
  * user — твой аккаунт (юзербот): только ЧИТАЕТ входящие личные сообщения.
           Он ничего не отправляет и не помечает сообщения прочитанными.
  * bot  — обычный бот из @BotFather: присылает тебе уведомления и показывает меню
           (Цели / Люди / Заметки). Отвечает только владельцу аккаунта.

Запуск: python bot.py   (перед этим один раз: python login.py)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError, RPCError
from telethon.tl.types import PeerUser, User

import texts
from settings import load_settings
from storage import Storage
from timeparse import parse_when

log = logging.getLogger("tgnotify")

# В режиме «от всех» не пересылаем служебные уведомления Telegram (там коды входа).
SKIP_IDS_ALL_MODE = {777000}
RECENT_LIMIT = 12
GOALS_SHOWN = 30
LOGIN_MARK = "login.ok"  # файл в data/, который создаёт login.py


class Ctx:
    """Общие объекты приложения (заполняются в amain)."""

    cfg = None
    db: Storage = None
    user: TelegramClient = None
    bot: TelegramClient = None
    owner_id: int = 0
    bot_id: int = 0
    state: tuple | None = None  # что бот ждёт от владельца: ("goal_when",) и т.п.
    ready: asyncio.Event = None


ctx = Ctx()


def B(label: str, data: str):
    return Button.inline(label, data.encode())


def now_tz() -> datetime:
    return datetime.now(ctx.cfg.tz)


# =====================================================================
#  Входящие сообщения (юзербот)
# =====================================================================
def describe(msg) -> str:
    """Текст сообщения; для вложений — пометка, к ней подпись (если есть)."""

    def g(name):
        return getattr(msg, name, None)

    text = (g("message") or "").strip()
    f = g("file")
    label = None
    if g("photo"):
        label = "🖼 Фото"
    elif g("voice"):
        label = "🎤 Голосовое сообщение"
    elif g("video_note"):
        label = "⭕ Видеосообщение"
    elif g("gif"):
        label = "🎞 GIF"
    elif g("video"):
        label = "🎬 Видео"
    elif g("sticker"):
        label = "🙂 Стикер " + (getattr(f, "emoji", None) or "")
    elif g("audio"):
        label = "🎵 Аудио"
    elif g("document"):
        label = "📎 Файл" + (f": {f.name}" if getattr(f, "name", None) else "")
    elif g("contact"):
        label = "👤 Контакт"
    elif g("geo") or g("venue"):
        label = "📍 Геопозиция"
    elif g("poll"):
        label = "📊 Опрос"
    elif g("media"):
        label = "📦 Вложение"
    label = label.strip() if label else None

    if label and text:
        return f"{label}\n{text}"
    return label or text or "(сообщение без текста)"


def sender_info(sender, fallback_id) -> tuple:
    """(имя, username) отправителя."""
    if sender is None:
        return str(fallback_id), None
    first = getattr(sender, "first_name", None)
    last = getattr(sender, "last_name", None)
    name = " ".join(p for p in (first, last) if p) or getattr(sender, "title", None)
    username = getattr(sender, "username", None)
    return (name or username or str(fallback_id)), username


async def on_new_message(event):
    await ctx.ready.wait()  # при догоне пропущенных сообщений ждём, пока всё запустится
    try:
        sender_id = event.sender_id
        if sender_id is None or sender_id in (ctx.owner_id, ctx.bot_id):
            return  # свои сообщения и сообщения нашего же бота (иначе будет петля)

        mode = ctx.db.get_setting("mode", "selected")
        if mode == "selected":
            if not ctx.db.is_person(sender_id):
                return
        else:
            if sender_id in SKIP_IDS_ALL_MODE:
                return

        sender = await event.get_sender()
        if mode == "all" and getattr(sender, "bot", False):
            return  # в режиме «от всех» чужие боты — это шум
        name, username = sender_info(sender, sender_id)

        msg = event.message
        row = {
            "chat_id": event.chat_id,
            "msg_id": msg.id,
            "sender_id": sender_id,
            "name": name,
            "username": username,
            "date_ts": int(msg.date.timestamp()),
            "orig_text": describe(msg),
            "edits": [],
        }
        sent = await ctx.bot.send_message(
            ctx.owner_id, texts.message_card(row, ctx.cfg.tz), parse_mode="html", link_preview=False
        )
        ctx.db.save_message(
            chat_id=row["chat_id"], msg_id=row["msg_id"], bot_msg_id=sent.id,
            sender_id=sender_id, name=name, username=username,
            date_ts=row["date_ts"], text=row["orig_text"],
        )
    except Exception:
        log.exception("Не удалось обработать входящее сообщение")


async def on_edited(event):
    await ctx.ready.wait()
    try:
        msg = event.message
        row = ctx.db.get_message(event.chat_id, msg.id)
        if row is None:
            return  # про это сообщение уведомления не было
        new_text = describe(msg)
        if new_text == row["cur_text"]:
            return

        edit_date = getattr(msg, "edit_date", None)
        edit = {
            "ts": int(edit_date.timestamp()) if edit_date else int(time.time()),
            "old": row["cur_text"],
            "new": new_text,
            "sep": False,
        }
        row["edits"].append(edit)
        card = texts.message_card(row, ctx.cfg.tz)

        sent_ok = False
        if len(card) <= texts.SAFE_LEN:
            try:
                await ctx.bot.edit_message(
                    ctx.owner_id, row["bot_msg_id"], card, parse_mode="html", link_preview=False
                )
                sent_ok = True
            except MessageNotModifiedError:
                sent_ok = True
            except RPCError as e:
                log.warning("Не получилось отредактировать карточку (%s), шлю отдельно", e)

        if not sent_ok:
            # Карточка не вмещает правку (или её удалили) — отдельное сообщение.
            edit["sep"] = True
            text = texts.edit_standalone(row, edit, ctx.cfg.tz)
            try:
                await ctx.bot.send_message(
                    ctx.owner_id, text, parse_mode="html", reply_to=row["bot_msg_id"], link_preview=False
                )
            except RPCError:
                await ctx.bot.send_message(ctx.owner_id, text, parse_mode="html", link_preview=False)

        ctx.db.update_message(event.chat_id, msg.id, new_text, row["edits"])
    except Exception:
        log.exception("Не удалось обработать правку сообщения")


# =====================================================================
#  Экраны меню
# =====================================================================
def screen_main():
    people = ctx.db.list_people()
    mode = ctx.db.get_setting("mode", "selected")
    mode_txt = "от всех" if mode == "all" else f"только от выбранных ({len(people)})"
    text = (
        "🏠 <b>Главное меню</b>\n\n"
        f"📩 Принимаю сообщения: {mode_txt}\n"
        f"🎯 Активных целей: {len(ctx.db.list_goals())}\n"
        f"📝 Заметок: {ctx.db.count_notes()}"
    )
    if mode != "all" and not people:
        text += "\n\n👥 Список людей пуст: добавь кого-нибудь в разделе «Люди», иначе уведомлений не будет."
    buttons = [[B("🎯 Цели", "g:menu")], [B("👥 Люди", "p:menu")], [B("📝 Заметки", "n:menu")]]
    return text, buttons


def screen_goals():
    goals = ctx.db.list_goals()[:GOALS_SHOWN]
    tz = ctx.cfg.tz
    lines = ["🎯 <b>Цели</b>", ""]
    if goals:
        for i, g in enumerate(goals, 1):
            due = datetime.fromtimestamp(g["due_ts"], tz)
            lines.append(f"{i}. {texts.date_short(due)} — {texts.esc(texts.clip(g['title'], 80))}")
        lines.append("\nНажми на цель ниже, чтобы удалить её.")
    else:
        lines.append("Пока пусто. Добавь цель — я напомню в нужный момент.")
    buttons = [[B("➕ Новая цель", "g:new")]]
    for i, g in enumerate(goals, 1):
        buttons.append([B(f"🗑 {i}. {texts.clip(g['title'], 28)}", f"g:del:{g['id']}")])
    buttons.append([B("◀️ Назад", "m")])
    return "\n".join(lines), buttons


def screen_goal_when():
    text = (
        "📅 <b>Новая цель</b>\n\n"
        "Напиши дату и, если нужно, время:\n"
        "• <code>25.12 18:30</code>\n"
        "• <code>завтра в 9:00</code>\n"
        "• <code>через 2 часа</code>\n"
        "• <code>15.01.2027</code>\n\n"
        f"Без времени поставлю {ctx.cfg.default_time[0]:02d}:{ctx.cfg.default_time[1]:02d}.\n"
        "Можно сразу с текстом: <code>25.12 18:30 купить подарок</code>"
    )
    return text, [[B("✖️ Отмена", "g:menu")]]


def screen_goal_title(due_ts: float):
    due = datetime.fromtimestamp(due_ts, ctx.cfg.tz)
    text = f"📅 {texts.date_short(due)}\n\n✍️ Что напомнить?"
    return text, [[B("✖️ Отмена", "g:menu")]]


def screen_people():
    people = ctx.db.list_people()
    mode = ctx.db.get_setting("mode", "selected")
    lines = [
        "👥 <b>Люди</b>",
        "",
        "Режим: " + ("📢 от всех" if mode == "all" else "🔒 только от выбранных"),
    ]
    if people:
        lines += ["", "Принимаю сообщения от:"]
        for i, p in enumerate(people, 1):
            lines.append(f"{i}. {texts.who(p['name'], p['username'])}")
        lines.append("\nНажми на человека ниже, чтобы убрать его.")
    else:
        lines += ["", "Список пуст."]
    toggle = "📢 Принимать от всех" if mode != "all" else "🔒 Только от выбранных"
    buttons = [[B("➕ Добавить человека", "p:add")], [B(toggle, "p:mode")]]
    for p in people:
        buttons.append([B(f"🗑 {texts.clip(p['name'], 30)}", f"p:del:{p['user_id']}")])
    buttons.append([B("◀️ Назад", "m")])
    return "\n".join(lines), buttons


def screen_person_add():
    text = (
        "➕ <b>Добавить человека</b>\n\n"
        "Пришли мне одно из:\n"
        "• <code>@username</code>\n"
        "• номер телефона <code>+79991234567</code> (если он в твоих контактах)\n"
        "• ID пользователя\n"
        "• любое сообщение от него, переслав сюда\n\n"
        "Или выбери из недавних чатов."
    )
    return text, [[B("📋 Из недавних чатов", "p:recent")], [B("✖️ Отмена", "p:menu")]]


async def screen_recent():
    existing = {p["user_id"] for p in ctx.db.list_people()}
    buttons, shown = [], 0
    try:
        async for d in ctx.user.iter_dialogs(limit=80):
            e = d.entity
            if not isinstance(e, User) or e.bot or e.is_self or e.deleted:
                continue
            if e.id in existing or e.id in SKIP_IDS_ALL_MODE or e.id == ctx.owner_id:
                continue
            name, username = sender_info(e, e.id)
            label = texts.clip(name, 28) + (f" @{username}" if username else "")
            buttons.append([B(label, f"p:pick:{e.id}")])
            shown += 1
            if shown >= RECENT_LIMIT:
                break
    except Exception:
        log.exception("Не удалось получить список диалогов")
    text = "📋 <b>Недавние чаты</b>\n\nВыбери человека:" if buttons else (
        "📋 В недавних чатах подходящих людей нет. Пришли @username или перешли сообщение."
    )
    buttons.append([B("◀️ Назад", "p:add")])
    return text, buttons


def screen_notes():
    notes = ctx.db.list_notes()
    total = ctx.db.count_notes()
    head = f"📝 <b>Заметки</b> ({total})"
    if total > len(notes):
        head += f"\nПоказаны последние {len(notes)}."
    if not notes:
        head += "\n\nПока пусто."
    tz = ctx.cfg.tz
    buttons = [[B("➕ Новая заметка", "n:new")]]
    for n in notes:
        first = n["text"].strip().splitlines()[0] if n["text"].strip() else "…"
        stamp = datetime.fromtimestamp(n["created_ts"], tz).strftime("%d.%m")
        buttons.append([B(f"📄 {stamp} · {texts.clip(first, 32)}", f"n:v:{n['id']}")])
    buttons.append([B("◀️ Назад", "m")])
    return head, buttons


def screen_note(note_id: int):
    n = ctx.db.get_note(note_id)
    if n is None:
        return screen_notes()
    dt = datetime.fromtimestamp(n["created_ts"], ctx.cfg.tz)
    text = f"📝 <b>Заметка</b> · {texts.date_short(dt)}\n\n{texts.esc(n['text'])}"
    return text, [[B("🗑 Удалить", f"n:del:{n['id']}")], [B("◀️ К заметкам", "n:menu")]]


def screen_note_new():
    return "✍️ Напиши текст заметки одним сообщением.", [[B("✖️ Отмена", "n:menu")]]


async def show(event, screen, *, edit: bool):
    text, buttons = screen
    kw = dict(buttons=buttons, parse_mode="html", link_preview=False)
    if edit:
        try:
            await event.edit(text, **kw)
        except MessageNotModifiedError:
            pass
    else:
        await event.respond(text, **kw)


# =====================================================================
#  Люди: поиск пользователя
# =====================================================================
async def get_user_by_id(uid: int):
    for attempt in (0, 1):
        try:
            ent = await ctx.user.get_entity(uid)
            return ent if isinstance(ent, User) else None
        except (ValueError, RPCError):
            if attempt == 0:  # аккаунт мог не «видеть» этого человека — освежим кэш диалогов
                try:
                    await ctx.user.get_dialogs(limit=200)
                except Exception:
                    pass
    return None


async def resolve_person(raw: str):
    t = re.sub(r"^(?:https?://)?(?:t\.me|telegram\.me)/", "", raw.strip(), flags=re.I).strip("/ ")
    if not t:
        return None
    try:
        if t.startswith("+"):
            ent = await ctx.user.get_entity(t)
        elif t.isdigit():
            ent = await get_user_by_id(int(t))
            if ent is None and len(t) >= 10:
                ent = await ctx.user.get_entity("+" + t)  # может, это номер без «+»
        else:
            ent = await ctx.user.get_entity(t.lstrip("@"))
    except (ValueError, TypeError, RPCError):
        return None
    return ent if isinstance(ent, User) else None


def add_person_entity(ent) -> str | None:
    """Добавляет человека. Возвращает текст ошибки или None, если всё хорошо."""
    if ent.id == ctx.owner_id:
        return "Это твой собственный аккаунт 🙂"
    if ent.id == ctx.bot_id:
        return "Это сам бот, его добавлять не нужно."
    name, username = sender_info(ent, ent.id)
    ctx.db.add_person(ent.id, name, username)
    return None


# =====================================================================
#  Кнопки меню
# =====================================================================
async def on_callback(event):
    if event.sender_id != ctx.owner_id:
        await event.answer()
        return
    await event.answer()
    data = event.data.decode("utf-8", "ignore")
    try:
        await route_callback(event, data)
    except Exception:
        log.exception("Ошибка в кнопке %s", data)


async def route_callback(event, data: str):
    ctx.state = None  # любая навигация отменяет ожидание ввода
    parts = data.split(":")

    if data == "m":
        await show(event, screen_main(), edit=True)

    # --- цели
    elif data == "g:menu":
        await show(event, screen_goals(), edit=True)
    elif data == "g:new":
        ctx.state = ("goal_when",)
        await show(event, screen_goal_when(), edit=True)
    elif parts[:2] == ["g", "del"]:
        ctx.db.delete_goal(int(parts[2]))
        await show(event, screen_goals(), edit=True)

    # --- люди
    elif data == "p:menu":
        await show(event, screen_people(), edit=True)
    elif data == "p:add":
        ctx.state = ("person",)
        await show(event, screen_person_add(), edit=True)
    elif data == "p:recent":
        ctx.state = ("person",)
        await show(event, await screen_recent(), edit=True)
    elif parts[:2] == ["p", "pick"]:
        ent = await get_user_by_id(int(parts[2]))
        if ent is not None:
            add_person_entity(ent)
        await show(event, screen_people(), edit=True)
    elif parts[:2] == ["p", "del"]:
        ctx.db.remove_person(int(parts[2]))
        await show(event, screen_people(), edit=True)
    elif data == "p:mode":
        mode = ctx.db.get_setting("mode", "selected")
        ctx.db.set_setting("mode", "selected" if mode == "all" else "all")
        await show(event, screen_people(), edit=True)

    # --- заметки
    elif data == "n:menu":
        await show(event, screen_notes(), edit=True)
    elif data == "n:new":
        ctx.state = ("note",)
        await show(event, screen_note_new(), edit=True)
    elif parts[:2] == ["n", "v"]:
        await show(event, screen_note(int(parts[2])), edit=True)
    elif parts[:2] == ["n", "del"]:
        ctx.db.delete_note(int(parts[2]))
        await show(event, screen_notes(), edit=True)


# =====================================================================
#  Текст от владельца в чате с ботом
# =====================================================================
def _create_goal_text(title: str, due: datetime) -> tuple:
    title = texts.clip(title.strip(), 500)
    ctx.db.add_goal(title, int(due.timestamp()))
    text = (
        "✅ <b>Цель добавлена</b>\n\n"
        f"🎯 {texts.esc(title)}\n"
        f"⏰ Напомню: {texts.date_short(due)}"
    )
    return text, [[B("🎯 К целям", "g:menu")], [B("🏠 Меню", "m")]]


async def on_text(event):
    if event.sender_id != ctx.owner_id:
        return  # чужим не отвечаем
    text = (event.raw_text or "").strip()

    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()
        if cmd in ("/start", "/menu", "/cancel"):
            ctx.state = None
            await show(event, screen_main(), edit=False)
        return

    st = ctx.state
    if st is None:
        await show(event, screen_main(), edit=False)
        return

    kind = st[0]

    if kind == "goal_when":
        now = now_tz()
        due, rest = parse_when(text, now, ctx.cfg.default_time)
        if due is None:
            await event.respond(
                "🤔 Не понял дату. Попробуй так: <code>25.12 18:30</code>, "
                "<code>завтра в 9:00</code> или <code>через 2 часа</code>.",
                parse_mode="html", buttons=[[B("✖️ Отмена", "g:menu")]],
            )
        elif due <= now:
            await event.respond(
                f"⌛ {texts.date_short(due)} — это время уже прошло. Напиши другую дату или время.",
                parse_mode="html", buttons=[[B("✖️ Отмена", "g:menu")]],
            )
        elif rest:
            ctx.state = None
            msg, buttons = _create_goal_text(rest, due)
            await event.respond(msg, parse_mode="html", buttons=buttons)
        else:
            ctx.state = ("goal_title", due.timestamp())
            await show(event, screen_goal_title(due.timestamp()), edit=False)

    elif kind == "goal_title":
        if not text:
            await event.respond("Нужен текст — что напомнить?", buttons=[[B("✖️ Отмена", "g:menu")]])
            return
        due = datetime.fromtimestamp(st[1], ctx.cfg.tz)
        if due <= now_tz():
            ctx.state = ("goal_when",)
            await event.respond("⌛ Пока ты писал, это время прошло. Выбери новое.", buttons=[[B("✖️ Отмена", "g:menu")]])
            return
        ctx.state = None
        msg, buttons = _create_goal_text(text, due)
        await event.respond(msg, parse_mode="html", buttons=buttons)

    elif kind == "person":
        ent, problem = None, None
        fwd = getattr(event.message, "fwd_from", None)
        if fwd is not None:
            from_id = getattr(fwd, "from_id", None)
            if isinstance(from_id, PeerUser):
                ent = await get_user_by_id(from_id.user_id)
            else:
                problem = "У этого человека скрыт профиль в пересылках — Telegram не отдаёт его ID."
        elif text:
            ent = await resolve_person(text)
        if ent is not None and problem is None:
            problem = add_person_entity(ent)
        elif problem is None:
            problem = (
                "Не нашёл такого пользователя. Проверь @username или выбери из недавних чатов."
            )
        if problem:
            await event.respond(
                f"⚠️ {problem}", parse_mode="html",
                buttons=[[B("📋 Из недавних чатов", "p:recent")], [B("✖️ Отмена", "p:menu")]],
            )
        else:
            ctx.state = None
            await show(event, screen_people(), edit=False)

    elif kind == "note":
        if not text:
            await event.respond("Нужен текст заметки.", buttons=[[B("✖️ Отмена", "n:menu")]])
            return
        ctx.state = None
        ctx.db.add_note(texts.clip(text, 3500))
        await show(event, screen_notes(), edit=False)


# =====================================================================
#  Фоновые задачи
# =====================================================================
async def fire_due_goals(now_ts: int | None = None) -> int:
    """Отправить напоминания по всем наступившим целям. Возвращает, сколько отправлено."""
    now_ts = int(time.time()) if now_ts is None else now_ts
    sent = 0
    for g in ctx.db.due_goals(now_ts):
        due = datetime.fromtimestamp(g["due_ts"], ctx.cfg.tz)
        late = now_ts - g["due_ts"] > 180
        await ctx.bot.send_message(
            ctx.owner_id, texts.goal_fired(g["title"], due, late), parse_mode="html"
        )
        ctx.db.mark_goal_fired(g["id"])  # только после успешной отправки
        sent += 1
    return sent


async def reminder_loop():
    last_warn = 0.0
    last_purge = time.time()
    while True:
        try:
            await fire_due_goals()
            if time.time() - last_purge > 3600:
                ctx.db.purge_old_messages()
                last_purge = time.time()
        except Exception as e:
            if time.time() - last_warn > 300:
                log.warning("Не получилось отправить напоминание, повторю: %s", e)
                last_warn = time.time()
        await asyncio.sleep(10)


async def notify_started():
    if not ctx.cfg.notify_on_start:
        return
    last = float(ctx.db.get_setting("last_start_msg", "0"))
    if time.time() - last < 600:  # не спамим при частых перезапусках
        return
    try:
        await ctx.bot.send_message(ctx.owner_id, "🟢 Бот запущен.\n\nМеню: /menu")
        ctx.db.set_setting("last_start_msg", time.time())
    except Exception as e:
        log.warning(
            "Не смог написать владельцу (%s). Открой бота в Telegram и нажми /start — "
            "без этого он не может присылать уведомления.", e,
        )


# =====================================================================
#  Запуск
# =====================================================================
async def amain(cfg) -> int:
    need_login = "Сессия аккаунта не найдена. Один раз запусти:  python login.py"
    # Метку ставит login.py после успешного входа. Файл .session для проверки не годится:
    # Telethon создаёт его пустым уже при создании клиента.
    if not (cfg.data_dir / LOGIN_MARK).exists():
        print(need_login, file=sys.stderr)  # быстро, не ходя в сеть
        return 2

    ctx.cfg = cfg
    ctx.db = Storage(cfg.data_dir / "bot.db")
    ctx.db.purge_old_messages()
    ctx.ready = asyncio.Event()

    common = dict(connection_retries=10, retry_delay=3)
    # sequential_updates: правка сообщения не может обогнать само сообщение.
    # catch_up: после перезапуска подтянуть то, что пришло, пока бот был выключен.
    ctx.user = TelegramClient(
        str(cfg.data_dir / "userbot"), cfg.api_id, cfg.api_hash,
        catch_up=True, sequential_updates=True,
        device_model="Message Notifier", app_version="1.0", **common,
    )
    ctx.bot = TelegramClient(
        str(cfg.data_dir / "bot"), cfg.api_id, cfg.api_hash, sequential_updates=True, **common
    )

    # Обработчики юзербота регистрируем ДО подключения, чтобы не потерять догоняемые сообщения.
    ctx.user.add_event_handler(on_new_message, events.NewMessage(incoming=True, func=lambda e: e.is_private))
    ctx.user.add_event_handler(on_edited, events.MessageEdited(incoming=True, func=lambda e: e.is_private))

    await ctx.user.connect()
    if not await ctx.user.is_user_authorized():  # вход был, но сеанс завершён в Telegram
        print(need_login, file=sys.stderr)
        return 2
    me = await ctx.user.get_me()
    ctx.owner_id = me.id

    await ctx.bot.start(bot_token=cfg.bot_token)
    ctx.bot_id = (await ctx.bot.get_me()).id

    ctx.bot.add_event_handler(on_callback, events.CallbackQuery())
    ctx.bot.add_event_handler(on_text, events.NewMessage(incoming=True, func=lambda e: e.is_private))

    ctx.ready.set()
    log.info("Запущено. Владелец: %s, бот: %s", ctx.owner_id, ctx.bot_id)
    await notify_started()

    tasks = [
        asyncio.create_task(ctx.user.run_until_disconnected()),
        asyncio.create_task(ctx.bot.run_until_disconnected()),
        asyncio.create_task(reminder_loop()),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for t in done:
        t.result()  # если причина — ошибка, она всплывёт в логе
    log.error("Соединение закрылось, выходим — systemd перезапустит.")
    return 1


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    cfg = load_settings()
    sys.exit(asyncio.run(amain(cfg)))


if __name__ == "__main__":
    main()
