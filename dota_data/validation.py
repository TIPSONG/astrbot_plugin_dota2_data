"""Deterministic identifiers and date windows, with no fuzzy guesses."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .errors import NeedsInput
from ..settings import validate

STEAM64_BASE = 76561197960265728


def checked_settings(values: dict) -> dict:
    return validate(dict(values))


def identity(value, name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} 不能为空或过长。")
    if any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} 含有不支持的控制字符。")
    return value.strip()


def account_id(value) -> int:
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,20}", text):
        raise ValueError("账号应为 Dota account ID 或数字 Steam64 ID。")
    number = int(text)
    if number >= STEAM64_BASE:
        number -= STEAM64_BASE
    if not 0 < number <= 4294967295:
        raise ValueError("Dota account ID 超出有效范围。")
    return number


def match_id(value) -> int:
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,13}", text) or int(text) <= 0:
        raise ValueError("请提供有效的数字比赛 ID。")
    return int(text)


def bounded_int(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数。")
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    if not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} 必须在 {low}–{high} 之间。")
    return value


def date_window(params: dict, zone: str) -> dict:
    tz = ZoneInfo(zone)
    today = datetime.now(tz).date()
    start_text, end_text = params.get("start_date"), params.get("end_date")
    period = str(params.get("period") or "7d").strip().lower()
    if start_text or end_text:
        if not start_text:
            raise NeedsInput("missing_start_date", "指定结束日期时，也请给出开始日期。")
        try:
            start = date.fromisoformat(str(start_text))
            end = date.fromisoformat(str(end_text)) if end_text else today
        except ValueError as exc:
            raise NeedsInput("invalid_date", "日期应为 YYYY-MM-DD。") from exc
        if end < start or start > today or (end - start).days > 3660:
            raise NeedsInput("invalid_window", "开始日期不能在未来，结束日期不能早于开始日期，查询跨度不超过十年。")
        end = min(end, today)
        period = "custom"
    elif period == "all":
        return {"period": "all", "timezone": zone, "start_date": None, "end_date": None,
                "start_ts": None, "end_ts_exclusive": None}
    else:
        monday = today - timedelta(days=today.weekday())
        choices = {
            "today": (today, today), "yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
            "this_week": (monday, today), "last_week": (monday - timedelta(days=7), monday - timedelta(days=1)),
            "7d": (today - timedelta(days=6), today), "30d": (today - timedelta(days=29), today),
        }
        if period not in choices:
            raise NeedsInput("invalid_period", "时间范围支持 today、yesterday、this_week、last_week、7d、30d、all 或明确起止日期。")
        start, end = choices[period]
    return {"period": period, "timezone": zone, "start_date": start.isoformat(), "end_date": end.isoformat(),
            "start_ts": int(datetime.combine(start, time.min, tzinfo=tz).timestamp()),
            "end_ts_exclusive": int(datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz).timestamp())}


def normalized(value: str) -> str:
    return "".join(value.strip().casefold().split())


# Narrow, explicit aliases. Unrecognised names return a clarification instead of a guess.
HERO_ALIASES = {
    "敌法师": 1, "敌法": 1, "am": 1, "斧王": 2, "axe": 2,
    "血魔": 4, "水晶室女": 5, "冰女": 5, "cm": 5,
    "撼地者": 7, "小牛": 7, "剑圣": 8, "主宰": 8, "jugg": 8,
    "变体精灵": 10, "水人": 10, "影魔": 11, "sf": 11,
    "帕吉": 14, "屠夫": 14, "风行者": 21, "风行": 21, "wr": 21,
    "莉娜": 25, "火女": 25, "莱恩": 26, "潮汐猎人": 29, "潮汐": 29,
    "狙击手": 35, "火枪": 35, "虚空假面": 41, "骷髅王": 42, "冥魂大帝": 42,
    "幻影刺客": 44, "pa": 44, "龙骑士": 49, "龙骑": 49, "dk": 49,
    "裂魂人": 71, "白牛": 71, "sb": 71, "祈求者": 74, "卡尔": 74,
    "拉比克": 86, "斯拉克": 93, "小鱼人": 93, "美杜莎": 94,
    "巨牙海民": 100, "海民": 100, "寒冬飞龙": 112, "冰龙": 112,
}
