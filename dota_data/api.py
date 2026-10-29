"""Read-only OpenDota adapter; no background polling or parse submissions."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from .errors import DataError
from .storage import Store, utc_now

BASE_URL = "https://api.opendota.com/api"


class OpenDota:
    def __init__(self, store: Store, settings: dict):
        self.store = store
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._minute: deque[float] = deque()

    async def get(self, path: str, params: dict | None = None, ttl: int | None = None) -> tuple[object, dict]:
        # Only service-created paths are allowed; never accept a URL from a model.
        if not path.startswith("/") or ":" in path or ".." in path or "?" in path:
            raise DataError("invalid_path", "查询路径无效。")
        clean = {key: value for key, value in (params or {}).items() if value is not None}
        cache_key = path + "|" + json.dumps(clean, sort_keys=True, separators=(",", ":"))
        async with self._lock:
            cached = self.store.cached(cache_key)
            if cached is not None:
                data, fetched_at = cached
                return data, {"fetched_at": fetched_at, "cached": True}
            now = time.monotonic()
            while self._minute and self._minute[0] <= now - 60:
                self._minute.popleft()
            if len(self._minute) >= self.settings["requests_per_minute"]:
                raise DataError("rate_limited", "已达到插件每分钟请求上限，请稍后再查。")
            day = datetime.now(ZoneInfo(self.settings["timezone"])).date().isoformat()
            if not self.store.reserve_request(day, self.settings["daily_request_limit"]):
                raise DataError("budget_exceeded", "已达到插件当日数据请求预算；已有有效缓存仍可读取。")
            self._minute.append(now)
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(headers={
                    "Accept": "application/json", "User-Agent": "AstrBot-Dota2-Data/0.1"
                })
            query = dict(clean)
            if self.settings.get("opendota_api_key"):
                query["api_key"] = self.settings["opendota_api_key"]
            try:
                async with self._session.get(
                    BASE_URL + path, params=query,
                    timeout=aiohttp.ClientTimeout(total=self.settings["timeout_seconds"]),
                    allow_redirects=False,
                ) as response:
                    if response.status == 429:
                        raise DataError("upstream_rate_limited", "OpenDota 暂时限流，请稍后再查。")
                    if response.status == 404:
                        raise DataError("not_found", "数据源未找到该记录；这不代表玩家没有进行过比赛。")
                    if response.status in (401, 403):
                        raise DataError("upstream_access_denied", "数据源拒绝访问，请检查接口权限或 Key。")
                    if response.status != 200:
                        raise DataError("upstream_error", f"OpenDota 返回 HTTP {response.status}。")
                    chunks = bytearray()
                    async for part in response.content.iter_chunked(65536):
                        chunks.extend(part)
                        if len(chunks) > 8 * 1024 * 1024:
                            raise DataError("response_too_large", "数据响应超过安全大小限制。")
                    data = json.loads(chunks)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise DataError("timeout", "数据请求超时；没有将超时当作空战绩。") from exc
            except (aiohttp.ClientError, ValueError, UnicodeError) as exc:
                # Exception text can contain credentials in URLs. Never expose it.
                raise DataError("network_error", "数据请求失败或响应不是有效 JSON。") from exc
            if not isinstance(data, (list, dict)):
                raise DataError("invalid_response", "数据源返回了不支持的数据格式。")
            if isinstance(data, dict) and data.get("error"):
                raise DataError("upstream_error", "数据源未能完成这次查询。")
            fetched_at = utc_now()
            self.store.put_cache(cache_key, data, ttl or self.settings["cache_ttl_seconds"], fetched_at)
            return data, {"fetched_at": fetched_at, "cached": False}

    async def close(self):
        async with self._lock:
            if self._session is not None and not self._session.closed:
                await self._session.close()
            self._session = None

