"""Разбор даты и времени из русского текста для целей-напоминаний.

Понимает, например:
    25.12 18:30            завтра в 9:00           через 2 часа
    15.01.2027             послезавтра 18.30       через 3 дня в 10:00
    2027-01-15 07:45       сегодня в 21            18:30 (ближайшее 18:30)
Если после даты/времени остался текст, он возвращается как «что напомнить».
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_F = re.I | re.U

_REL = re.compile(
    r"(?:\b(?:на|к)\s+)?\bчерез\s+(?:(?P<n>\d+)\s*)?"
    r"(?P<u>полчаса|мин\w*|час\w*|ч|дн\w*|ден\w*|сут\w*|нед\w*|м|д)(?![а-яё])",
    _F,
)
_WORD = re.compile(r"(?:\b(?:на|к)\s+)?\b(?P<w>послезавтра|завтра|сегодня)\b", _F)
_ISO = re.compile(r"(?:\b(?:на|к)\s+)?(?<!\d)(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})(?!\d)", _F)
_DMY = re.compile(
    r"(?:\b(?:на|к)\s+)?(?<![\d:.])(?P<d>\d{1,2})[./](?P<m>\d{1,2})"
    r"(?:[./](?P<y>\d{4}|\d{2}))?(?![\d:])",
    _F,
)
# 18:30, в 18:30, в 9:05
_TIME_COLON = re.compile(r"(?:\bв\s*)?(?<![\d.:])(?P<h>\d{1,2}):(?P<mi>\d{2})(?![\d:])", _F)
# 18.30 (только двузначный час, чтобы не путать с «2.5 кг»)
_TIME_DOT = re.compile(r"(?:\bв\s*)?(?<![\d.:])(?P<h>[01]\d|2[0-3])\.(?P<mi>[0-5]\d)(?![\d.])", _F)
# «в 9», «в 18 часов»
_TIME_H = re.compile(r"\bв\s+(?P<h>\d{1,2})(?:\s*(?:ч\b|час\w*))?(?![\d:.])", _F)


def _cut(s: str, m: re.Match) -> str:
    return s[: m.start()] + " " + s[m.end():]


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip(" \t-–—:,;.")


def _valid_date(y: int, m: int, d: int):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _find_time(s: str):
    """Возвращает (час, минута, строка_без_времени) или None."""
    for rx in (_TIME_COLON, _TIME_DOT, _TIME_H):
        m = rx.search(s)
        if not m:
            continue
        h = int(m.group("h"))
        mi = int(m.group("mi")) if "mi" in rx.groupindex else 0
        if h < 24 and mi < 60:
            return h, mi, _cut(s, m)
    return None


def parse_when(text: str, now: datetime, default_time=(9, 0)):
    """Разобрать текст. Возвращает (datetime | None, остаток_текста).

    `now` — текущее время с часовым поясом; результат в том же поясе.
    Если дата не распознана, вернётся (None, исходный_текст).
    """
    s = text.strip()
    tz = now.tzinfo
    d = None  # найденная дата

    # --- 1. «через N ...»
    m = _REL.search(s)
    if m:
        n = int(m.group("n")) if m.group("n") else 1
        u = m.group("u").lower()
        s = _cut(s, m)
        try:
            if u.startswith("полчаса"):
                return (now + timedelta(minutes=30)).replace(second=0, microsecond=0), _clean(s)
            if u.startswith("мин") or u == "м":
                return (now + timedelta(minutes=n)).replace(second=0, microsecond=0), _clean(s)
            if u.startswith("час") or u == "ч":
                return (now + timedelta(hours=n)).replace(second=0, microsecond=0), _clean(s)
            days = n * 7 if u.startswith("нед") else n
            d = (now + timedelta(days=days)).date()
        except OverflowError:
            return None, text.strip()

    # --- 2. сегодня / завтра / послезавтра
    if d is None:
        m = _WORD.search(s)
        if m:
            shift = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[m.group("w").lower()]
            d = now.date() + timedelta(days=shift)
            s = _cut(s, m)

    # --- 3. 2027-01-15
    if d is None:
        for m in _ISO.finditer(s):
            cand = _valid_date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
            if cand:
                d = cand
                s = _cut(s, m)
                break

    # --- 4. 25.12 / 25.12.2026 / 25.12.26
    if d is None:
        for m in _DMY.finditer(s):
            day, mon, ys = int(m.group("d")), int(m.group("m")), m.group("y")
            if ys:
                year = int(ys) + (2000 if len(ys) == 2 else 0)
                cand = _valid_date(year, mon, day)
            else:
                cand = _valid_date(now.year, mon, day)
                if cand and cand < now.date():
                    cand = _valid_date(now.year + 1, mon, day)
            if cand:
                d = cand
                s = _cut(s, m)
                break

    # --- 5. время
    found = _find_time(s)
    if found:
        h, mi, s = found
        t = (h, mi)
    else:
        t = None

    if d is None and t is None:
        return None, text.strip()

    if d is None:  # только время: ближайшее наступление
        result = datetime(now.year, now.month, now.day, t[0], t[1], tzinfo=tz)
        if result <= now:
            result += timedelta(days=1)
        return result, _clean(s)

    hh, mm = t if t else default_time
    result = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
    return result, _clean(s)
