"""Framework-independent structured Dota queries.

The service cannot speak, call an LLM, change a persona, or read another
plugin's database. It returns facts and their coverage for the existing agent.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .api import OpenDota
from .errors import DataError, NeedsInput
from .storage import Store, utc_now
from .validation import (
    HERO_ALIASES, account_id, bounded_int, checked_settings, date_window,
    identity, match_id, normalized,
)

log = logging.getLogger(__name__)
ACTIONS = {"profile", "matches", "match", "hero_pool", "compare", "meta"}
SELF_NAMES = {"", "me", "self", "我", "本人", "自己"}
MATCH_FILTERS = {"hero", "mode", "outcome", "limit", "period", "start_date", "end_date"}
PARAMETERS = {
    "profile": {"target", "player"},
    "matches": {"target", "player"} | MATCH_FILTERS,
    "hero_pool": {"target", "player"} | MATCH_FILTERS,
    "match": {"match_id"},
    "compare": {"targets", "players", "common_matches_only"} | MATCH_FILTERS,
    "meta": {"hero", "rank", "min_matches", "limit"},
}


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def text(value, limit=100):
    return str(value or "")[:limit]


def won(row: dict):
    slot, radiant = number(row.get("player_slot")), row.get("radiant_win")
    if slot is None or not isinstance(radiant, bool):
        return None
    return (slot < 128) == radiant


def average(rows: list[dict], field: str):
    values = [number(row.get(field)) for row in rows]
    values = [value for value in values if value is not None]
    return round(sum(values) / len(values), 2) if values else None


def summarize(rows: list[dict]) -> dict:
    results = [won(row) for row in rows]
    known = [win for win in results if win is not None]
    wins = sum(1 for win in known if win)
    return {
        "matches": len(rows), "known_outcomes": len(known), "wins": wins,
        "losses": len(known) - wins, "unknown_outcomes": len(rows) - len(known),
        "win_rate_percent": round(100 * wins / len(known), 2) if known else None,
        "average_kills": average(rows, "kills"), "average_deaths": average(rows, "deaths"),
        "average_assists": average(rows, "assists"), "average_gpm": average(rows, "gold_per_min"),
        "average_xpm": average(rows, "xp_per_min"), "average_last_hits": average(rows, "last_hits"),
        "average_duration_seconds": average(rows, "duration"),
        "metric_sample_counts": {key: sum(number(row.get(key)) is not None for row in rows)
                                 for key in ("kills", "deaths", "assists", "gold_per_min", "xp_per_min", "last_hits", "duration")},
    }


class DataService:
    def __init__(self, data_dir: Path, settings: dict):
        self.settings = checked_settings(settings)
        self.store = Store(Path(data_dir))
        self.api = OpenDota(self.store, self.settings)
        # Serialize bounded queries; never launch unattended refresh jobs.
        self._query_lock = asyncio.Lock()
        self._closed = False

    def update_settings(self, settings: dict):
        merged = dict(self.settings)
        merged.update(settings)
        updated = checked_settings(merged)
        self.settings.clear()
        self.settings.update(updated)

    def status(self) -> dict:
        day = datetime.now(ZoneInfo(self.settings["timezone"])).date().isoformat()
        return {**self.store.status(day), "enabled": self.settings["enabled"],
                "source": "OpenDota", "timezone": self.settings["timezone"],
                "requests_per_minute": self.settings["requests_per_minute"],
                "daily_request_limit": self.settings["daily_request_limit"],
                "network_status": "not_probed", "active_query": self._query_lock.locked()}

    def bindings(self) -> list[dict]:
        return self.store.bindings()

    def save_binding(self, scope: str, user_id: str, account_id_value=None, alias: str = "", **kwargs) -> dict:
        # Accept the named account_id argument used by panel handlers too.
        if account_id_value is None:
            account_id_value = kwargs.get("account_id")
        scope = identity(scope, "会话标识")
        user_id = identity(user_id, "用户标识", 128)
        aid = account_id(account_id_value)
        if not isinstance(alias, str) or len(alias) > 40 or any(ord(c) < 32 for c in alias):
            raise ValueError("别名应为不超过 40 字符的普通文本。")
        alias = alias.strip()
        if alias and (normalized(alias) in SELF_NAMES or alias.isdecimal()):
            raise ValueError("别名不能使用“我”、me 或纯数字账号。")
        for existing in self.store.bindings(scope):
            if alias and existing["user_id"] != user_id and normalized(existing["alias"]) == normalized(alias):
                raise ValueError("这个会话已使用该别名，请换一个明确的别名。")
        return self.store.bind(scope, user_id, aid, alias)

    def remove_binding(self, scope: str, user_id: str) -> dict:
        return {"removed": self.store.unbind(identity(scope, "会话标识"), identity(user_id, "用户标识", 128))}

    def recent_queries(self) -> list[dict]:
        return self.store.recent_queries()

    def clear_cache(self) -> dict:
        return self.store.clear_cache()

    async def close(self):
        self._closed = True
        async with self._query_lock:
            await self.api.close()

    def _resolve(self, target, scope: str, user_id: str) -> int:
        if target is None:
            target = "me"
        if isinstance(target, bool) or not isinstance(target, (str, int)):
            raise NeedsInput("invalid_target", "请提供明确的玩家别名或数字账号。")
        raw = str(target).strip()
        if len(raw) > 80:
            raise NeedsInput("invalid_target", "玩家标识过长，请用账号 ID 或已绑定的别名。")
        if raw.isdecimal():
            return account_id(raw)
        rows = self.store.bindings(scope)
        if normalized(raw) in SELF_NAMES:
            hits = [row for row in rows if row["user_id"] == user_id]
            if not hits:
                raise NeedsInput("binding_required", "当前用户在这个会话尚未绑定 Dota 账号，请先提供数字账号，或完成账号绑定。")
        else:
            hits = [row for row in rows if row["alias"] and normalized(row["alias"]) == normalized(raw)]
            if not hits:
                raise NeedsInput("unknown_player", "当前会话没有这个玩家别名，请提供 Dota/Steam64 数字 ID 或先建立绑定。")
        ids = {row["account_id"] for row in hits}
        if len(ids) != 1:
            raise NeedsInput("ambiguous_player", "该名字对应多个账号，请提供明确的数字账号。")
        return ids.pop()

    async def query(self, action: str, params: dict, scope: str, user_id: str) -> dict:
        start = time.monotonic()
        result = {"status": "error", "data": None, "meta": {}}
        try:
            if not isinstance(action, str) or action not in ACTIONS:
                raise NeedsInput("unknown_action", "不支持的查询类型。")
            if not isinstance(params, dict):
                raise NeedsInput("invalid_params", "查询参数必须为对象。")
            if set(params) - PARAMETERS[action]:
                raise NeedsInput("unsupported_parameters", "该查询包含不支持的参数，请仅使用此查询类型已声明的条件。")
            if ("player" in params and "target" in params) or ("players" in params and "targets" in params):
                raise NeedsInput("conflicting_targets", "请只使用 target 或 targets 提供玩家，不要同时使用其旧别名参数。")
            scope, user_id = identity(scope, "会话标识"), identity(user_id, "用户标识", 128)
            if not self.settings["enabled"] or self._closed:
                raise DataError("disabled", "Dota 数据查询当前已关闭。")
            if self._query_lock.locked():
                raise DataError("busy", "已有数据查询正在执行，请等它返回后再查。")
            async with self._query_lock:
                result = await asyncio.wait_for(self._dispatch(action, params, scope, user_id), timeout=45)
        except NeedsInput as exc:
            result = {"status": "needs_input", "code": exc.code, "message": exc.message, "data": None, "meta": {}}
        except ValueError as exc:
            result = {"status": "needs_input", "code": "invalid_parameter", "message": str(exc), "data": None, "meta": {}}
        except DataError as exc:
            result = {"status": "unavailable", "code": exc.code, "message": exc.message, "data": None, "meta": {}}
        except (asyncio.TimeoutError, TimeoutError):
            result = {"status": "unavailable", "code": "query_deadline", "message": "本次查询达到 45 秒总时限，请减少玩家数量、场次或缩短日期范围后重试。", "data": None, "meta": {}}
        except Exception as exc:
            # Keep private values and request URLs out of log records and tool results.
            log.warning("Dota data query failed: %s", type(exc).__name__)
            result = {"status": "error", "code": "internal_error", "message": "查询内部错误，本次没有可用结果。", "data": None, "meta": {}}
        result.setdefault("meta", {}).update({"source": "OpenDota", "queried_at": utc_now(),
                                               "timezone": self.settings["timezone"], "data_only": True})
        try:
            self.store.record_query(action if isinstance(action, str) and action in ACTIONS else "invalid", result["status"], int((time.monotonic() - start) * 1000))
        except sqlite3.Error:
            log.warning("Dota query summary could not be recorded.")
        return result

    async def _dispatch(self, action: str, params: dict, scope: str, user_id: str) -> dict:
        if action == "profile":
            return await self._profile(params, scope, user_id)
        if action in {"matches", "hero_pool"}:
            return await self._player_matches(action, params, scope, user_id)
        if action == "match":
            return await self._match(params)
        if action == "compare":
            return await self._compare(params, scope, user_id)
        return await self._meta(params)

    async def _heroes(self) -> tuple[dict[int, dict], dict]:
        rows, stamp = await self.api.get("/heroes", ttl=86400)
        if not isinstance(rows, list):
            raise DataError("invalid_response", "英雄字典格式异常。")
        return {row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), int)}, stamp

    def _hero_id(self, value, catalog: dict[int, dict]) -> int | None:
        if value is None or value == "" or value == "all":
            return None
        name = normalized(str(value))
        candidate = HERO_ALIASES.get(name)
        if name.isdecimal():
            candidate = int(name)
        if candidate in catalog:
            return candidate
        hits = []
        for hid, row in catalog.items():
            names = {normalized(text(row.get("localized_name"))),
                     normalized(text(row.get("name")).removeprefix("npc_dota_hero_")),
                     normalized(text(row.get("name")).removeprefix("npc_dota_hero_").replace("_", " "))}
            if name in names:
                hits.append(hid)
        if len(hits) == 1:
            return hits[0]
        raise NeedsInput("unknown_hero", "未能唯一识别英雄，请给出官方英文名、明确的中文名或英雄 ID。")

    def _filters(self, params: dict, catalog: dict[int, dict]) -> dict:
        mode = str(params.get("mode") or "all").lower()
        outcome = str(params.get("outcome") or "all").lower()
        if mode not in {"all", "ranked", "normal", "turbo"}:
            raise NeedsInput("invalid_mode", "模式支持 all、ranked、normal、turbo。")
        if outcome not in {"all", "win", "loss"}:
            raise NeedsInput("invalid_outcome", "胜负条件支持 all、win、loss。")
        return {"hero_id": self._hero_id(params.get("hero"), catalog), "mode": mode, "outcome": outcome,
                "limit": bounded_int(params.get("limit", self.settings["max_matches"]), "limit", 1, self.settings["max_matches"]),
                "window": date_window(params, self.settings["timezone"])}

    async def _scan(self, aid: int, filters: dict) -> tuple[list[dict], dict]:
        base = {}
        if filters["hero_id"] is not None:
            base["hero_id"] = filters["hero_id"]
        if filters["mode"] == "ranked":
            base["lobby_type"] = 7
        elif filters["mode"] == "normal":
            base["lobby_type"] = 0
        elif filters["mode"] == "turbo":
            base["game_mode"] = 23
        if filters["outcome"] != "all":
            base["win"] = int(filters["outcome"] == "win")
        start_ts = filters["window"]["start_ts"]
        end_ts = filters["window"]["end_ts_exclusive"]
        rows, seen, stamps = [], set(), []
        scanned, missing_times = 0, 0
        reason = "scan_limit"
        while scanned < self.settings["scan_limit"]:
            size = min(100, self.settings["scan_limit"] - scanned)
            page, stamp = await self.api.get(f"/players/{aid}/matches", {**base, "limit": size, "offset": scanned})
            stamps.append(stamp)
            if not isinstance(page, list):
                raise DataError("invalid_response", "战绩列表格式异常。")
            scanned += len(page)
            older = False
            for row in page:
                if not isinstance(row, dict) or not isinstance(row.get("match_id"), int):
                    continue
                ts = number(row.get("start_time"))
                if ts is None:
                    missing_times += 1
                    continue
                if start_ts is not None and ts < start_ts:
                    older = True
                    continue
                if end_ts is not None and ts >= end_ts:
                    continue
                if filters["hero_id"] is not None and row.get("hero_id") != filters["hero_id"]:
                    continue
                if filters["mode"] == "ranked" and row.get("lobby_type") != 7:
                    continue
                if filters["mode"] == "normal" and (row.get("lobby_type") != 0 or row.get("game_mode") == 23):
                    continue
                if filters["mode"] == "turbo" and row.get("game_mode") != 23:
                    continue
                if filters["outcome"] != "all" and won(row) is not (filters["outcome"] == "win"):
                    continue
                if row["match_id"] not in seen:
                    seen.add(row["match_id"])
                    rows.append(row)
            if older:
                reason = "requested_start_reached"
                break
            if len(page) < size:
                reason = "available_history_exhausted"
                break
        rows.sort(key=lambda row: (row.get("start_time", 0), row["match_id"]), reverse=True)
        complete = reason != "scan_limit" and missing_times == 0
        return rows, {"scanned_rows": scanned, "matched_rows": len(rows), "scan_limit": self.settings["scan_limit"],
                      "stop_reason": reason, "available_window_complete": complete,
                      "missing_time_rows": missing_times, "pages": len(stamps),
                      "cached_pages": sum(stamp["cached"] for stamp in stamps),
                      "oldest_fetch_at": min((stamp["fetched_at"] for stamp in stamps), default=None),
                      "newest_fetch_at": max((stamp["fetched_at"] for stamp in stamps), default=None)}

    def _row(self, row: dict, heroes: dict[int, dict]) -> dict:
        fields = ("match_id", "hero_id", "start_time", "duration", "game_mode", "lobby_type", "player_slot",
                  "kills", "deaths", "assists", "gold_per_min", "xp_per_min", "last_hits", "hero_damage",
                  "tower_damage", "hero_healing", "lane_role")
        result = {field: number(row.get(field)) for field in fields}
        result.update({"hero_name": text(heroes.get(row.get("hero_id"), {}).get("localized_name")), "won": won(row)})
        if result["match_id"]:
            result["source_url"] = f"https://www.opendota.com/matches/{int(result['match_id'])}"
        return result

    async def _profile(self, params: dict, scope: str, user_id: str) -> dict:
        aid = self._resolve(params.get("target", params.get("player", "me")), scope, user_id)
        row, stamp = await self.api.get(f"/players/{aid}")
        if not isinstance(row, dict):
            raise DataError("invalid_response", "玩家资料格式异常。")
        profile = row.get("profile") if isinstance(row.get("profile"), dict) else {}
        return {"status": "ok" if profile else "empty", "data": {
            "account_id": aid, "personaname": text(profile.get("personaname")),
            "rank_tier": number(row.get("rank_tier")), "leaderboard_rank": number(row.get("leaderboard_rank")),
            "last_match_time": text(row.get("last_match_time")),
            "source_url": f"https://www.opendota.com/players/{aid}"},
            "meta": {**stamp, "limitations": ["段位字段是来源记录，不是实时 MMR；公开资料缺失不能证明账号不存在。"]}}

    async def _player_matches(self, action: str, params: dict, scope: str, user_id: str) -> dict:
        aid = self._resolve(params.get("target", params.get("player", "me")), scope, user_id)
        heroes, _ = await self._heroes()
        filters = self._filters(params, heroes)
        all_rows, coverage = await self._scan(aid, filters)
        rows = all_rows[:filters["limit"]]
        coverage.update({"returned_rows": len(rows), "statistics_rows": len(rows),
                         "result_limit": filters["limit"], "result_truncated": len(all_rows) > len(rows),
                         "statistics_cover_available_window": coverage["available_window_complete"] and len(rows) == len(all_rows)})
        data = {"account_id": aid, "summary": summarize(rows)}
        if action == "hero_pool":
            grouped = defaultdict(list)
            for row in rows:
                grouped[row.get("hero_id")].append(row)
            data["heroes"] = [{"hero_id": hid, "hero_name": text(heroes.get(hid, {}).get("localized_name")),
                               **summarize(group)} for hid, group in grouped.items()]
            data["heroes"].sort(key=lambda row: row["matches"], reverse=True)
        else:
            data["matches"] = [self._row(row, heroes) for row in rows]
        return {"status": "ok" if rows else "empty", "data": data,
                "meta": {"filters": filters, "coverage": coverage,
                         "limitations": ["统计仅覆盖返回的最近符合条件的对局；数量上限之外的对局未计入。",
                                          "公开 API 可能缺少或延迟收录比赛；空结果不等于没有打过比赛。",
                                          "normal 表示普通大厅且排除加速；all 会混合模式，比较时请指定模式。"]}}

    async def _match(self, params: dict) -> dict:
        mid = match_id(params.get("match_id"))
        row, stamp = await self.api.get(f"/matches/{mid}")
        if not isinstance(row, dict) or not row.get("match_id"):
            raise DataError("not_found", "暂未取得该对局详情。")
        heroes, _ = await self._heroes()
        raw_players = row.get("players") if isinstance(row.get("players"), list) else []
        players = []
        for raw in raw_players[:10]:
            if not isinstance(raw, dict):
                continue
            enriched = {**raw, "match_id": mid, "start_time": row.get("start_time"),
                        "duration": row.get("duration"), "radiant_win": row.get("radiant_win"),
                        "game_mode": row.get("game_mode"), "lobby_type": row.get("lobby_type")}
            player = self._row(enriched, heroes)
            player["account_id"] = number(raw.get("account_id"))
            player["personaname"] = text(raw.get("personaname"))
            player["items"] = [number(raw.get(f"item_{n}")) for n in range(6)]
            player["purchase_log"] = [{"time": number(item.get("time")), "item_key": text(item.get("key"), 60)}
                                      for item in (raw.get("purchase_log") or [])[:50] if isinstance(item, dict)]
            players.append(player)
        gold = row.get("radiant_gold_adv")
        xp = row.get("radiant_xp_adv")
        gold = [number(value) for value in gold[:180]] if isinstance(gold, list) else []
        xp = [number(value) for value in xp[:180]] if isinstance(xp, list) else []
        parsed = isinstance(row.get("version"), int) and row["version"] > 0
        return {"status": "ok", "data": {"match_id": mid, "start_time": number(row.get("start_time")),
                "duration_seconds": number(row.get("duration")), "radiant_win": row.get("radiant_win") if isinstance(row.get("radiant_win"), bool) else None,
                "radiant_score": number(row.get("radiant_score")), "dire_score": number(row.get("dire_score")),
                "patch_id": number(row.get("patch")), "players": players,
                "radiant_gold_advantage_by_minute": gold, "radiant_xp_advantage_by_minute": xp,
                "source_url": f"https://www.opendota.com/matches/{mid}"},
                "meta": {**stamp, "parsed": parsed, "available_sections": {"scoreboard": bool(players), "economy_curve": bool(gold), "xp_curve": bool(xp)},
                         "limitations": ["只返回数据源已有字段；未解析时不能推断逐分钟过程，不会提交付费或耗额解析请求。",
                                          "出装记录最多每人 50 项，曲线最多 180 分钟；缺失值不是零。",
                                          "不能仅凭 KDA 判断责任或证明某次操作失误。"]}}

    async def _compare(self, params: dict, scope: str, user_id: str) -> dict:
        targets = params.get("targets", params.get("players"))
        if not isinstance(targets, list) or not 2 <= len(targets) <= 6:
            raise NeedsInput("invalid_targets", "比较需要 2–6 个明确的玩家账号或已绑定别名。")
        ids = [self._resolve(target, scope, user_id) for target in targets]
        if len(set(ids)) != len(ids):
            raise NeedsInput("duplicate_targets", "比较名单中存在相同账号，请提供不同玩家。")
        common = params.get("common_matches_only", False)
        if not isinstance(common, bool):
            raise NeedsInput("invalid_common_flag", "common_matches_only 必须为布尔值。")
        heroes, _ = await self._heroes()
        filters = self._filters(params, heroes)
        collections, coverage = {}, {}
        # Sequential requests honor one upstream budget; no fan-out to dozens of calls.
        for aid in ids:
            collections[aid], coverage[aid] = await self._scan(aid, filters)
        common_ids = set.intersection(*({row["match_id"] for row in collections[aid]} for aid in ids)) if common else None
        output = []
        for aid in ids:
            eligible = collections[aid]
            if common_ids is not None:
                eligible = [row for row in eligible if row["match_id"] in common_ids]
            rows = eligible[:filters["limit"]]
            details = {**coverage[aid], "returned_rows": len(rows), "result_truncated": len(eligible) > len(rows),
                       "statistics_rows": len(rows), "statistics_cover_available_window": coverage[aid]["available_window_complete"] and len(eligible) == len(rows)}
            output.append({"account_id": aid, "summary": summarize(rows), "coverage": details,
                           "match_ids": [row["match_id"] for row in rows]})
        return {"status": "ok" if any(item["summary"]["matches"] for item in output) else "empty",
                "data": {"players": output, "common_matches_only": common},
                "meta": {"filters": filters, "limitations": [
                    "同一时间范围不保证双方英雄、位置、分段相同；均值只能描述样本，不自动评定谁更强。",
                    "共同对局仅按比赛 ID 交集确定，不能证明同队或组队；扫描上限可能遗漏共同对局。",
                    "任一数据源请求失败时整次比较返回不可用，避免用零战绩替代缺失玩家。"]}}

    async def _meta(self, params: dict) -> dict:
        heroes, _ = await self._heroes()
        hid = self._hero_id(params.get("hero"), heroes)
        rank = bounded_int(params.get("rank", 0), "rank", 0, 8)
        minimum = bounded_int(params.get("min_matches", 100), "min_matches", 1, 10000000)
        limit = bounded_int(params.get("limit", min(10, self.settings["max_matches"])), "limit", 1, self.settings["max_matches"])
        data, stamp = await self.api.get("/heroStats", ttl=1800)
        if not isinstance(data, list):
            raise DataError("invalid_response", "英雄统计格式异常。")
        result = []
        for row in data:
            if not isinstance(row, dict) or (hid is not None and row.get("id") != hid):
                continue
            ranks = [rank] if rank else list(range(1, 9))
            picks = [number(row.get(f"{r}_pick")) for r in ranks]
            wins = [number(row.get(f"{r}_win")) for r in ranks]
            # Do not invent a zero for an absent rank bucket.
            if any(value is None for value in picks + wins):
                continue
            total, victories = sum(picks), sum(wins)
            if total < minimum or victories < 0 or victories > total:
                continue
            result.append({"hero_id": row.get("id"), "hero_name": text(row.get("localized_name")),
                           "matches": total, "wins": victories, "win_rate_percent": round(100 * victories / total, 2),
                           "roles": [text(role, 30) for role in row.get("roles", [])]})
        result.sort(key=lambda row: (row["win_rate_percent"], row["matches"]), reverse=True)
        return {"status": "ok" if result else "empty", "data": {"heroes": result[:limit], "rank_bucket": rank,
                "minimum_matches": minimum}, "meta": {**stamp, "source_url": "https://www.opendota.com/heroes",
                "coverage": {"eligible_heroes": len(result), "returned_heroes": len(result[:limit])},
                "limitations": ["统计窗口与补丁边界未由响应明确给出，不能将此榜称为当前补丁的纯样本。",
                                 "这里只提供聚合胜率，不代表个人适合度、出装建议或位置专属胜率。"]}}

