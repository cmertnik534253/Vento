"""Настройки из переменных окружения / файла .env."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    tz: ZoneInfo
    default_time: tuple  # (час, минута) для напоминаний, где время не указано
    data_dir: Path
    notify_on_start: bool


def _need(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"В .env не заполнено поле {name}. Смотри .env.example")
    return value


def _parse_hhmm(raw: str) -> tuple:
    try:
        h, m = raw.strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
        return h, m
    except ValueError:
        sys.exit(f"DEFAULT_REMINDER_TIME должен быть вида 09:00, а не {raw!r}")


def load_settings(need_bot: bool = True) -> Settings:
    api_id_raw = _need("TG_API_ID")
    if not api_id_raw.isdigit():
        sys.exit("TG_API_ID должен быть числом (my.telegram.org -> API development tools)")

    data_dir = Path(os.getenv("DATA_DIR") or BASE_DIR / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        data_dir.chmod(0o700)  # в папке лежит сессия аккаунта
    except OSError:
        pass

    tz_name = os.getenv("TZ_NAME", "Europe/Moscow").strip() or "Europe/Moscow"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        sys.exit(f"Неизвестный часовой пояс TZ_NAME={tz_name!r}")

    return Settings(
        api_id=int(api_id_raw),
        api_hash=_need("TG_API_HASH"),
        bot_token=_need("BOT_TOKEN") if need_bot else "",
        tz=tz,
        default_time=_parse_hhmm(os.getenv("DEFAULT_REMINDER_TIME", "09:00")),
        data_dir=data_dir,
        notify_on_start=os.getenv("NOTIFY_ON_START", "1").strip() not in ("0", "false", "no"),
    )
