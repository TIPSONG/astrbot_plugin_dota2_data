"""Configuration validation; never reads or edits another plugin's settings."""
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULTS = {
    "enabled": True,
    "timeout_seconds": 15,
    "cache_ttl_seconds": 300,
    "max_matches": 50,
    "scan_limit": 500,
    "requests_per_minute": 30,
    "daily_request_limit": 1000,
    "timezone": "Asia/Hong_Kong",
    "opendota_api_key": "",
}
RANGES = {
    "timeout_seconds": (3, 60), "cache_ttl_seconds": (30, 86400),
    "max_matches": (1, 100), "scan_limit": (50, 2000),
    "requests_per_minute": (1, 60), "daily_request_limit": (1, 2000),
}


def validate(values: dict) -> dict:
    if not isinstance(values, dict):
        raise ValueError("配置必须是对象。")
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise ValueError("包含不支持的配置字段。")
    result = {**DEFAULTS, **values}
    if type(result["enabled"]) is not bool:
        raise ValueError("enabled 必须是布尔值。")
    for key, (low, high) in RANGES.items():
        value = result[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} 必须是 {low} 到 {high} 之间的整数。")
    if result["scan_limit"] < result["max_matches"]:
        raise ValueError("扫描上限不能小于单次结果上限。")
    zone = result["timezone"]
    if not isinstance(zone, str) or len(zone) > 80:
        raise ValueError("时区格式不正确。")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("未知时区；Windows 环境需具备 tzdata 时区数据。") from None
    key = result["opendota_api_key"]
    if not isinstance(key, str) or len(key) > 512 or any(ord(c) < 32 for c in key):
        raise ValueError("OpenDota Key 格式不正确。")
    result["opendota_api_key"] = key.strip()
    return result

