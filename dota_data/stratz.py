"""Read-only STRATZ GraphQL adapter, normalised to the service's data contract.

Only fixed, named ``query`` operations are sent. There is no raw GraphQL input,
mutation, parse submission, model call, message handler or persona operation.

Field references (source review only; this implementation has not been run):
* Official API entry point: https://stratz.com/api
* Public generated schema models: TheAmazingLooser/STRATZ_Models,
  b83999ee652a2dd5d630ce593e9928dc85110696, PlayerMatchesRequestType,
  MatchPlayerType, MatchType, SteamAccountType and their enum types.
* Behaviour reference: mml-hrb/astrbot-plugin-dota2matches,
  a9fafa6ec65d7d32b08c7eba4fffd1839bfa4d29/dota_stratz.py.

This is independent adapter code. Schema names are API contracts, not a copy of
the reference plugin's implementation. Schema compatibility still needs later
runtime verification with an authorised token; no API probe is made on import.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .errors import DataError
from .storage import Store, utc_now

ENDPOINT = "https://api.stratz.com/graphql"
MAX_BODY_BYTES = 8 * 1024 * 1024

PROFILE_QUERY = """
query DotaDataProfile($id: Long!) {
  player(steamAccountId: $id) {
    steamAccountId
    steamAccount {
      id name seasonRank seasonLeaderboardRank lastMatchDateTime isAnonymous
    }
  }
}
"""

PLAYER_FIELDS = """
    steamAccountId playerSlot isRadiant isVictory heroId
    kills deaths assists goldPerMinute experiencePerMinute numLastHits
    heroDamage towerDamage heroHealing lane
"""

MATCHES_QUERY = """
query DotaDataMatches($id: Long!, $request: PlayerMatchesRequestType!) {
  player(steamAccountId: $id) {
    matches(request: $request) {
      id startDateTime durationSeconds didRadiantWin gameMode lobbyType
      parsedDateTime
      players(steamAccountId: $id) { PLAYER_FIELDS }
    }
  }
}
""".replace("PLAYER_FIELDS", PLAYER_FIELDS)

MATCH_QUERY = """
query DotaDataMatch($id: Long!) {
  match(id: $id) {
    id startDateTime durationSeconds didRadiantWin gameMode lobbyType
    parsedDateTime radiantKills direKills radiantNetworthLeads
    radiantExperienceLeads gameVersionId
    players {
      PLAYER_FIELDS
      steamAccount { name }
      item0Id item1Id item2Id item3Id item4Id item5Id
    }
  }
}
""".replace("PLAYER_FIELDS", PLAYER_FIELDS)

# Enumerated numeric IDs shared with Dota's game/lobby modes. Unknown enum
# additions remain None; they are never converted into an existing mode.
GAME_MODES = {
    "NONE": 0, "ALL_PICK": 1, "CAPTAINS_MODE": 2, "RANDOM_DRAFT": 3,
    "SINGLE_DRAFT": 4, "ALL_RANDOM": 5, "INTRO": 6, "THE_DIRETIDE": 7,
    "REVERSE_CAPTAINS_MODE": 8, "THE_GREEVILING": 9, "TUTORIAL": 10,
    "MID_ONLY": 11, "LEAST_PLAYED": 12, "NEW_PLAYER_POOL": 13,
    "COMPENDIUM_MATCHMAKING": 14, "CUSTOM": 15, "CAPTAINS_DRAFT": 16,
    "BALANCED_DRAFT": 17, "ABILITY_DRAFT": 18, "EVENT": 19,
    "ALL_RANDOM_DEATH_MATCH": 20, "SOLO_MID": 21,
    "ALL_PICK_RANKED": 22, "TURBO": 23, "MUTATION": 24,
}
LOBBIES = {
    "UNRANKED": 0, "PRACTICE": 1, "TOURNAMENT": 2, "TUTORIAL": 3,
    "COOP_VS_BOTS": 4, "TEAM_MATCH": 5, "SOLO_QUEUE": 6, "RANKED": 7,
    "SOLO_MID": 8, "BATTLE_CUP": 9, "EVENT": 10,
}
LANES = {"SAFE_LANE": 1, "MID_LANE": 2, "OFF_LANE": 3, "JUNGLE": 4}
PLAYER_NUMBERS = {
    "account_id": "steamAccountId", "hero_id": "heroId",
    "kills": "kills", "deaths": "deaths", "assists": "assists",
    "gold_per_min": "goldPerMinute", "xp_per_min": "experiencePerMinute",
    "last_hits": "numLastHits", "hero_damage": "heroDamage",
    "tower_damage": "towerDamage", "hero_healing": "heroHealing",
}


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _enum(value: Any, mapping: dict[str, int]) -> int | None:
    if isinstance(value, str):
        return mapping.get(value.upper())
    if type(value) is int and value in mapping.values():
        return value
    return None


def _integer(value: Any, label: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise DataError("unsupported_filter", f"STRATZ 的 {label} 参数超出支持范围。")
    return value


def _iso(timestamp: Any) -> str | None:
    stamp = _number(timestamp)
    if stamp is None or not 0 < stamp < 4102444800:
        return None
    try:
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    except (OverflowError, ValueError, OSError):
        return None


def _array(value: Any) -> list[int | float | None] | None:
    if not isinstance(value, list):
        return None
    return [_number(item) for item in value]


def _kill_total(value: Any) -> int | float | None:
    values = _array(value)
    if not values or any(item is None or item < 0 for item in values):
        return None
    return sum(values)


def _match_common(match: dict) -> dict:
    mid = match.get("id")
    if type(mid) is not int or mid <= 0:
        raise DataError("invalid_response", "STRATZ 对局缺少有效比赛编号。")
    parsed_at = _number(match.get("parsedDateTime"))
    return {
        "match_id": mid,
        "start_time": _number(match.get("startDateTime")),
        "duration": _number(match.get("durationSeconds")),
        "radiant_win": _boolean(match.get("didRadiantWin")),
        "game_mode": _enum(match.get("gameMode"), GAME_MODES),
        "lobby_type": _enum(match.get("lobbyType"), LOBBIES),
        "_parsed": parsed_at > 0 if parsed_at is not None else None,
        "_source": "STRATZ",
        "source_url": f"https://stratz.com/matches/{mid}",
    }


def _player(player: dict, radiant_win: bool | None) -> dict:
    out = {target: _number(player.get(source)) for target, source in PLAYER_NUMBERS.items()}
    radiant = _boolean(player.get("isRadiant"))
    victory = _boolean(player.get("isVictory"))
    if radiant is not None and radiant_win is not None and victory is not None:
        if victory != (radiant == radiant_win):
            raise DataError("invalid_response", "STRATZ 胜负与阵营字段不一致，未使用这份结果。")
    # STRATZ's own playerSlot is retained, but never assumed to use OpenDota's
    # bit encoding. The derived marker is sufficient only for team/win checks.
    out["stratz_player_slot"] = _number(player.get("playerSlot"))
    out["player_slot"] = (0 if radiant else 128) if radiant is not None else None
    out["_player_slot_side_only"] = True
    out["_player_win"] = victory
    out["_is_radiant"] = radiant
    out["lane_role"] = LANES.get(str(player.get("lane") or "").upper())
    account = player.get("steamAccount")
    out["personaname"] = str(account.get("name") or "")[:100] if isinstance(account, dict) else None
    for index in range(6):
        out[f"item_{index}"] = _number(player.get(f"item{index}Id"))
    return out


class Stratz:
    """Fixed-schema data adapter. ``budget.reserve()`` must be synchronous."""

    def __init__(self, store: Store, settings: dict, budget):
        self.store = store
        self.settings = settings
        self.budget = budget
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    def _operation(self, path: str, params: dict) -> tuple[str, dict, str, int, dict]:
        if not isinstance(path, str):
            raise DataError("unsupported_endpoint", "STRATZ 不支持此查询路径。")
        found = re.fullmatch(r"/players/([0-9]{1,10})(/matches)?", path)
        if found:
            aid = _integer(int(found.group(1)), "account_id", 1, 4294967295)
            if not found.group(2):
                if params:
                    raise DataError("unsupported_filter", "STRATZ 玩家资料查询不支持这些参数。")
                return PROFILE_QUERY, {"id": aid}, "profile", aid, {}
            allowed = {"limit", "offset", "hero_id", "lobby_type", "game_mode", "win"}
            if set(params) - allowed:
                raise DataError("unsupported_filter", "STRATZ 战绩查询包含不支持的筛选条件。")
            take = _integer(params.get("limit", 100), "limit", 1, 100)
            skip = _integer(params.get("offset", 0), "offset", 0, 100000)
            request = {"take": take, "skip": skip, "orderBy": "DESC"}
            canonical = {"limit": take, "offset": skip}
            if "hero_id" in params:
                hero = _integer(params["hero_id"], "hero_id", 1, 32767)
                request["heroIds"] = [hero]
                canonical["hero_id"] = hero
            for name, field, values in (("game_mode", "gameModeIds", GAME_MODES),
                                         ("lobby_type", "lobbyTypeIds", LOBBIES)):
                if name in params:
                    value = _integer(params[name], name, 0, 255)
                    if value not in values.values():
                        raise DataError("unsupported_filter", f"STRATZ 暂不支持这个 {name} 编号。")
                    request[field] = [value]
                    canonical[name] = value
            if "win" in params:
                victory = _integer(params["win"], "win", 0, 1)
                request["isVictory"] = victory == 1
                canonical["win"] = victory
            return MATCHES_QUERY, {"id": aid, "request": request}, "matches", aid, canonical
        found = re.fullmatch(r"/matches/([0-9]{1,13})", path)
        if found:
            mid = _integer(int(found.group(1)), "match_id", 1, 9999999999999)
            if params:
                raise DataError("unsupported_filter", "STRATZ 单场详情查询不支持这些参数。")
            return MATCH_QUERY, {"id": mid}, "match", mid, {}
        raise DataError("unsupported_endpoint", "此数据类型由 OpenDota 提供，STRATZ 适配器未声明支持。")

    @staticmethod
    def _profile(data: dict, aid: int) -> dict:
        player = data.get("player")
        if not isinstance(player, dict):
            raise DataError("not_found", "STRATZ 未提供该玩家资料。")
        account = player.get("steamAccount")
        if not isinstance(account, dict):
            raise DataError("not_found", "STRATZ 未提供可读取的账号资料。")
        if account.get("isAnonymous") is True:
            raise DataError("upstream_access_denied", "STRATZ 标记该账号为匿名，未输出其资料。")
        source_id = player.get("steamAccountId")
        if type(source_id) is not int or source_id != aid:
            raise DataError("invalid_response", "STRATZ 返回的账号与请求不一致。")
        return {"profile": {"account_id": aid, "personaname": str(account.get("name") or "")[:100]},
                "rank_tier": _number(account.get("seasonRank")),
                "leaderboard_rank": _number(account.get("seasonLeaderboardRank")),
                "last_match_time": _iso(account.get("lastMatchDateTime")),
                "_source": "STRATZ", "source_url": f"https://stratz.com/players/{aid}"}

    @staticmethod
    def _matches(data: dict, aid: int, params: dict) -> list[dict]:
        player = data.get("player")
        if not isinstance(player, dict):
            raise DataError("not_found", "STRATZ 未提供该玩家战绩，不能据此判断没有比赛。")
        matches = player.get("matches")
        if not isinstance(matches, list) or len(matches) > params["limit"]:
            raise DataError("invalid_response", "STRATZ 战绩分页响应格式异常。")
        result = []
        for match in matches:
            if not isinstance(match, dict) or not isinstance(match.get("players"), list):
                raise DataError("invalid_response", "STRATZ 战绩缺少玩家数据，未将缺失行计作空结果。")
            candidates = [row for row in match["players"] if isinstance(row, dict) and row.get("steamAccountId") == aid]
            if len(candidates) != 1:
                raise DataError("invalid_response", "STRATZ 未能唯一对应指定玩家的对局表现。")
            row = _match_common(match)
            row.update(_player(candidates[0], row["radiant_win"]))
            if row["radiant_win"] is None and row["_player_win"] is not None and row["_is_radiant"] is not None:
                row["radiant_win"] = row["_player_win"] == row["_is_radiant"]
            if row["start_time"] is None:
                raise DataError("invalid_response", "STRATZ 未提供比赛时间，无法保持日期筛选口径。")
            for name in ("hero_id", "game_mode", "lobby_type"):
                if name in params and row[name] != params[name]:
                    raise DataError("invalid_response", "STRATZ 返回的数据不满足请求的筛选条件。")
            if "win" in params:
                victory = row["_player_win"]
                if victory is None and row["radiant_win"] is not None and row["_is_radiant"] is not None:
                    victory = row["radiant_win"] == row["_is_radiant"]
                if victory is None or victory != (params["win"] == 1):
                    raise DataError("invalid_response", "STRATZ 未能确认胜负筛选，未把结果作为有效战绩。")
            result.append(row)
        if len({row["match_id"] for row in result}) != len(result):
            raise DataError("invalid_response", "STRATZ 战绩分页中包含重复比赛。")
        # Do not reorder a badly ordered page: the scanner relies on a globally
        # descending stream to stop at the lower date boundary.
        if any(result[n]["start_time"] < result[n + 1]["start_time"] for n in range(len(result) - 1)):
            raise DataError("invalid_response", "STRATZ 战绩没有按时间倒序返回。")
        return result

    @staticmethod
    def _match(data: dict, mid: int) -> dict:
        match = data.get("match")
        if not isinstance(match, dict):
            raise DataError("not_found", "STRATZ 暂未提供该对局详情。")
        common = _match_common(match)
        if common["match_id"] != mid:
            raise DataError("invalid_response", "STRATZ 返回了不同的比赛编号。")
        players = match.get("players")
        if not isinstance(players, list) or len(players) > 10 or any(not isinstance(row, dict) for row in players):
            raise DataError("invalid_response", "STRATZ 参赛者数据格式异常。")
        common.update({
            "players": [_player(row, common["radiant_win"]) for row in players],
            "radiant_score": _kill_total(match.get("radiantKills")),
            "dire_score": _kill_total(match.get("direKills")),
            "radiant_gold_adv": _array(match.get("radiantNetworthLeads")),
            "radiant_xp_adv": _array(match.get("radiantExperienceLeads")),
            # This namespace is not substituted for OpenDota's patch ID.
            "stratz_game_version_id": _number(match.get("gameVersionId")),
        })
        return common

    async def get(self, path: str, params: dict | None = None, ttl: int | None = None) -> tuple[object, dict]:
        if params is not None and not isinstance(params, dict):
            raise DataError("unsupported_filter", "STRATZ 查询参数必须为对象。")
        clean = {key: value for key, value in (params or {}).items() if value is not None}
        query, variables, kind, identifier, canonical = self._operation(path, clean)
        cache_key = "stratz:v1:" + path + "|" + json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        url = f"https://stratz.com/{'matches' if kind == 'match' else 'players'}/{identifier}"
        async with self._lock:
            token = self.settings.get("stratz_api_key", "")
            if not isinstance(token, str) or not token.strip():
                raise DataError("missing_key", "尚未配置 STRATZ Token。")
            token = token.strip()
            if any(ord(char) < 32 for char in token):
                raise DataError("upstream_access_denied", "STRATZ Token 格式无效。")
            cached = self.store.cached(cache_key)
            if cached is not None:
                payload, fetched_at = cached
                return payload, {"source": "STRATZ", "fetched_at": fetched_at, "cached": True, "source_url": url}
            self.budget.reserve()
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(headers={"Accept": "application/json", "User-Agent": "STRATZ_API"})
            try:
                async with self._session.post(
                    ENDPOINT,
                    headers={"Authorization": "Bearer " + token},
                    json={"query": query, "variables": variables},
                    timeout=aiohttp.ClientTimeout(total=self.settings["timeout_seconds"]),
                    allow_redirects=False,
                ) as response:
                    if response.status in (401, 403):
                        raise DataError("upstream_access_denied", "STRATZ 拒绝访问，请检查 Token、接口权限或网络出口。")
                    if response.status == 429:
                        raise DataError("upstream_rate_limited", "STRATZ 暂时限流。")
                    if response.status != 200:
                        raise DataError("upstream_error", f"STRATZ 返回 HTTP {response.status}。")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > MAX_BODY_BYTES:
                            raise DataError("response_too_large", "STRATZ 响应超过大小限制。")
                    envelope = json.loads(body)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise DataError("timeout", "STRATZ 数据请求超时。") from exc
            except (aiohttp.ClientError, ValueError, UnicodeError) as exc:
                # No raw HTTP exceptions, headers, GraphQL errors or tokens are
                # emitted to the caller or logs.
                raise DataError("network_error", "STRATZ 请求失败或响应不是有效 JSON。") from exc
            if not isinstance(envelope, dict):
                raise DataError("invalid_response", "STRATZ 响应格式异常。")
            if envelope.get("errors"):
                # Reject partial GraphQL data as a unit. Unknown fields must not
                # silently remove filters or erase individual players.
                raise DataError("schema_error", "STRATZ 未完整执行查询，可能为接口字段、权限或请求限制变化。")
            data = envelope.get("data")
            if not isinstance(data, dict):
                raise DataError("invalid_response", "STRATZ 响应缺少数据对象。")
            if kind == "profile":
                payload = self._profile(data, identifier)
            elif kind == "matches":
                payload = self._matches(data, identifier, canonical)
            else:
                payload = self._match(data, identifier)
            fetched_at = utc_now()
            self.store.put_cache(cache_key, payload, ttl or self.settings["cache_ttl_seconds"], fetched_at)
            return payload, {"source": "STRATZ", "fetched_at": fetched_at, "cached": False, "source_url": url}

    async def close(self):
        async with self._lock:
            if self._session is not None and not self._session.closed:
                await self._session.close()
            self._session = None
