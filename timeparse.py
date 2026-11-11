"""Форматирование сообщений бота (HTML для Telegram)."""
from __future__ import annotations

import html
from datetime import datetime

WD_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WD_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

# Лимит Telegram на текст сообщения — 4096. Считаем по HTML-строке с запасом.
SAFE_LEN = 3900
MAX_TEXT_IN_CARD = 3000


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def clip(s: str, limit: int) -> str:
    s = s or ""
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def date_long(dt: datetime) -> str:
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}, {WD_FULL[dt.weekday()]}"


def date_short(dt: datetime) -> str:
    return f"{dt:%d.%m.%Y %H:%M} ({WD_SHORT[dt.weekday()]})"


def who(name: str, username: str | None) -> str:
    return f"{esc(name)} (@{esc(username)})" if username else esc(name)


def _edit_stamp(edit_dt: datetime, orig_dt: datetime) -> str:
    if edit_dt.date() == orig_dt.date():
        return f"{edit_dt:%H:%M}"
    return f"{edit_dt:%d.%m %H:%M}"


def _edited_body(old: str, new: str) -> str:
    """Новый текст; если к старому просто дописали — дописанное выделяется жирным."""
    old, new = clip(old, MAX_TEXT_IN_CARD), clip(new, MAX_TEXT_IN_CARD)
    if old and new != old and new.startswith(old):
        return esc(old) + "<b>" + esc(new[len(old):]) + "</b>"
    return esc(new)


def edit_block(edit: dict, orig_dt: datetime, tz) -> str:
    edit_dt = datetime.fromtimestamp(edit["ts"], tz)
    return (
        f"✏️ <b>Изменено на</b> ({_edit_stamp(edit_dt, orig_dt)}):\n"
        f"<blockquote>{_edited_body(edit['old'], edit['new'])}</blockquote>"
    )


def edit_standalone(row: dict, edit: dict, tz) -> str:
    """Правка, которая не влезла в исходную карточку: отдельным сообщением."""
    orig_dt = datetime.fromtimestamp(row["date_ts"], tz)
    return f"✏️ <b>{esc(row['name'])}</b> изменил(а) сообщение от {orig_dt:%d.%m %H:%M}\n\n" + edit_block(
        edit, orig_dt, tz
    )


def message_card(row: dict, tz) -> str:
    """Карточка входящего сообщения + все правки, которые в неё поместились."""
    dt = datetime.fromtimestamp(row["date_ts"], tz)
    text = (
        "📩 <b>Новое сообщение</b>\n\n"
        f"👤 <b>Кто писал:</b> {who(row['name'], row.get('username'))}\n"
        f"📅 <b>Дата:</b> {date_long(dt)}\n"
        f"🕒 <b>Время:</b> {dt:%H:%M}\n"
        f"💬 <b>Что писал:</b>\n"
        f"<blockquote>{esc(clip(row['orig_text'], MAX_TEXT_IN_CARD))}</blockquote>"
    )
    for edit in row.get("edits", []):
        if not edit.get("sep"):
            text += "\n\n" + edit_block(edit, dt, tz)
    return text


def goal_fired(title: str, due_dt: datetime, late: bool) -> str:
    text = (
        "⏰ <b>Время пришло!</b>\n\n"
        f"🎯 {esc(title)}\n"
        f"🕒 Запланировано на: {date_short(due_dt)}"
    )
    if late:
        text += "\n\n⚠️ Напоминание задержалось: бот был недоступен."
    return text
