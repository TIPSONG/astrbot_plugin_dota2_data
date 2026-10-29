"""One shared request allowance for all data sources, including fallback."""
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from .errors import DataError


class RequestBudget:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self._minute = deque()

    def reserve(self):
        # Synchronous reservation contains no await: adapters share one counter.
        now = time.monotonic()
        while self._minute and self._minute[0] <= now - 60:
            self._minute.popleft()
        if len(self._minute) >= self.settings["requests_per_minute"]:
            raise DataError("rate_limited", "已达到双数据源共享的每分钟请求预算。")
        day = datetime.now(ZoneInfo(self.settings["timezone"])).date().isoformat()
        if not self.store.reserve_request(day, self.settings["daily_request_limit"]):
            raise DataError("budget_exceeded", "已达到双数据源共享的当日请求预算；已有有效缓存仍可读取。")
        self._minute.append(now)

