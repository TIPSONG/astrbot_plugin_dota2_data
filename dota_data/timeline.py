"""Personal item timings and economic snapshots from recorded match events."""
import json
from pathlib import Path

from .errors import NeedsInput


ITEMS = {int(k): names for k, names in json.loads(
    Path(__file__).with_name("item_names.json").read_text(encoding="utf-8")
).items()}
ITEM_KEYS = {names[0]: item_id for item_id, names in ITEMS.items()}
ALIASES = {"跳刀": "blink", "blink dagger": "blink", "黑皇": "black_king_bar",
           "bkb": "black_king_bar", "假腿": "power_treads"}


def resolve_item(value):
    if value is None:
        return None
    name = str(value).strip().casefold().removeprefix("item_")
    if name.isascii() and name.isdecimal() and 0 < len(name) <= 6 and int(name) > 0:
        return int(name)
    name = ALIASES.get(name, name)
    matches = [i for i, names in ITEMS.items() if name and name in (s.casefold() for s in names)]
    if len(matches) != 1:
        raise NeedsInput("unknown_item", "无法唯一识别装备，请提供装备全名或数字物品 ID。")
    return matches[0]


def purchases(player):
    events = player.get("purchase_log")
    if not isinstance(events, list):
        return None
    rows = []
    for event in events:
        if not isinstance(event, dict) or type(event.get("time")) is not int:
            continue
        key = event.get("key")
        item = event.get("item_id")
        if type(item) is not int:
            item = ITEM_KEYS.get(key) if isinstance(key, str) else None
        if item is None and not isinstance(key, str):
            continue
        names = ITEMS.get(item, ())
        rows.append({"time": event["time"], "item_id": item, "item_key": names[0] if names else key,
                     "item_name": names[2] if names else key})
    return sorted(rows, key=lambda r: r["time"])


def _value(row, field, index):
    values = row.get(field)
    if isinstance(values, list) and index < len(values):
        value = values[index]
        if type(value) in (int, float) and value >= 0:
            return value
    return None


def player_timeline(player, participants, duration, item_id):
    events = purchases(player)
    first = next((e for e in events or [] if e["item_id"] == item_id), None) if item_id else None
    cutoff = first["time"] if first else None
    # Without an observed purchase there is no defensible end to the requested phase.
    end = cutoff if item_id else duration
    visible = [] if item_id and cutoff is None else [
        e for e in events or [] if end is None or e["time"] <= end]
    networth = player.get("networth_per_minute") or []
    hits = player.get("last_hits_each_minute") or []
    minutes = []
    total_hits = 0 if hits else None
    for m in range(min(181, max(len(networth), len(hits) + 1 if hits else 0))):
        if end is None or m * 60 > end:
            break
        if m and total_hits is not None:
            bucket = _value(player, "last_hits_each_minute", m - 1)
            total_hits = total_hits + bucket if bucket is not None else None
        worth = _value(player, "networth_per_minute", m)
        peers = [_value(p, "networth_per_minute", m) for p in participants]
        rank = None
        if worth is not None and len(peers) == 10 and None not in peers:
            rank = 1 + sum(v > worth for v in peers)
        minutes.append({"minute": m, "net_worth": worth, "rank_of_10": rank, "last_hits": total_hits})
    return {
        "hero_id": player.get("hero_id"), "account_id": player.get("account_id"),
        "purchase_log_available": events is not None,
        "until_item_id": item_id, "first_purchase_seconds": cutoff,
        "cutoff_status": ("whole_match" if not item_id else "observed" if first else
                          "purchase_log_unavailable" if events is None else "no_matching_record"),
        "purchases": visible[:200], "purchase_count": len(visible), "purchases_truncated": len(visible) > 200,
        "minutes": minutes, "last_snapshot": minutes[-1] if minutes else None,
    }
