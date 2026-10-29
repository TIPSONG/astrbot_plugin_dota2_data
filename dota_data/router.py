"""Source selection without mixing offset pagination from different providers."""
import re

from .api import OpenDota
from .budget import RequestBudget
from .errors import DataError
from .stratz import Stratz

LABELS = {"opendota": "OpenDota", "stratz": "STRATZ"}
NO_FALLBACK = {"rate_limited", "budget_exceeded", "invalid_path", "invalid_parameter"}


class DataAPI:
    def __init__(self, store, settings):
        self.settings = settings
        budget = RequestBudget(store, settings)
        self.clients = {"opendota": OpenDota(store, settings, budget), "stratz": Stratz(store, settings, budget)}
        self.begin_query()

    def begin_query(self):
        self._pinned = {}
        self.trace = []

    async def _read(self, name, path, params, ttl, fallback=None):
        try:
            data, stamp = await self.clients[name].get(path, params=params, ttl=ttl)
        except DataError as exc:
            self.trace.append({"source": LABELS[name], "path": path,
                               "role": "dictionary" if path == "/heroes" else "data",
                               "status": "unavailable", "code": exc.code,
                               **({"fallback": fallback} if fallback else {})})
            raise
        stamp = {**stamp, "source": LABELS[name]}
        self.trace.append({**stamp, "path": path, "status": "ok",
                           "role": "dictionary" if path == "/heroes" else "data",
                           **({"fallback": fallback} if fallback else {})})
        return data, stamp

    async def get(self, path, params=None, ttl=None):
        # The two services' hero aggregates have different populations and windows.
        # Keep these explicit rather than silently substituting an unlike metric.
        if path in {"/heroes", "/heroStats"}:
            return await self._read("opendota", path, params, ttl)
        paged = bool(re.fullmatch(r"/players/[0-9]+/matches", path))
        if paged and path in self._pinned:
            return await self._read(self._pinned[path], path, params, ttl)
        primary = self.settings["data_source_priority"]
        secondary = "stratz" if primary == "opendota" else "opendota"
        try:
            result = await self._read(primary, path, params, ttl)
            chosen = primary
        except DataError as original:
            if (not self.settings["fallback_enabled"] or original.code in NO_FALLBACK
                    or (secondary == "stratz" and not self.settings["stratz_api_key"])):
                raise
            detail = {"from": LABELS[primary], "to": LABELS[secondary], "reason": original.code}
            result = await self._read(secondary, path, params, ttl, fallback=detail)
            chosen = secondary
        if paged:
            self._pinned[path] = chosen
        return result

    async def close(self):
        try:
            await self.clients["opendota"].close()
        finally:
            await self.clients["stratz"].close()

