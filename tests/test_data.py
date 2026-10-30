"""Offline behavioral tests. All databases and HTTP servers are disposable."""
import asyncio
import json
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo
from scratch import TestDirectory

from aiohttp import web

from astrbot_plugin_dota2_data.settings import DEFAULTS, validate
from astrbot_plugin_dota2_data.dota_data import api as od_module, stratz as st_module
from astrbot_plugin_dota2_data.dota_data.api import OpenDota
from astrbot_plugin_dota2_data.dota_data.budget import RequestBudget
from astrbot_plugin_dota2_data.dota_data.errors import DataError, NeedsInput
from astrbot_plugin_dota2_data.dota_data.router import DataAPI
from astrbot_plugin_dota2_data.dota_data.service import DataService, summarize
from astrbot_plugin_dota2_data.dota_data.storage import Store
from astrbot_plugin_dota2_data.dota_data.stratz import Stratz
from astrbot_plugin_dota2_data.dota_data.validation import account_id, date_window

STAMP = {"source": "OpenDota", "cached": False, "fetched_at": "2026-10-30T00:00:00+00:00"}
HEROES = [{"id": 2, "localized_name": "Axe", "name": "npc_dota_hero_axe"}]


def match(mid, **extra):
    return {"match_id": mid, "start_time": 1793318400 - mid, "hero_id": 2,
            "game_mode": 22, "lobby_type": 7, "player_slot": 0, "radiant_win": True,
            "kills": 6, "deaths": 2, "assists": 4, **extra}


def stratz_match(mid=123):
    return {"id": mid, "startDateTime": 1793318400, "durationSeconds": 2400,
            "didRadiantWin": True, "gameMode": "ALL_PICK_RANKED", "lobbyType": "RANKED",
            "parsedDateTime": 1793321000, "radiantKills": [2, 3], "direKills": [1, 2],
            "gameVersionId": 999, "radiantNetworthLeads": [0, 120],
            "players": [{"steamAccountId": 42, "playerSlot": 5, "isRadiant": False,
                         "isVictory": False, "heroId": 2, "kills": 3, "deaths": 4,
                         "assists": 6, "lane": "OFF_LANE"}]}


class TempTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TestDirectory()
        self.settings = dict(DEFAULTS)
        self.store = Store(Path(self.tmp.name))
        self.service = DataService(Path(self.tmp.name), self.settings)

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()


class ValidationTests(unittest.TestCase):
    def test_account_boundaries_and_steam64(self):
        self.assertEqual(account_id("76561197960265770"), 42)
        for value in (0, -1, True, "1e6", "https://example.com", 4294967296):
            with self.subTest(value=value), self.assertRaises(ValueError):
                account_id(value)

    def test_strict_settings(self):
        for change in ({"enabled": 1}, {"scan_limit": 50, "max_matches": 100},
                       {"data_source_priority": "custom"}, {"url": "https://example.com"},
                       {"stratz_api_key": "bad\nheader"}, {"timezone": "unknown/zone"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate(change)

    def test_hong_kong_date_boundaries(self):
        import astrbot_plugin_dota2_data.dota_data.validation as validation
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 1, 0, 1, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        with patch.object(validation, "datetime", Clock):
            y = date_window({"period": "yesterday"}, "Asia/Hong_Kong")
            self.assertEqual((y["start_date"], y["end_date"]), ("2026-09-30", "2026-09-30"))
            self.assertEqual(y["end_ts_exclusive"] - y["start_ts"], 86400)
            week = date_window({"period": "last_week"}, "Asia/Hong_Kong")
            self.assertEqual((week["start_date"], week["end_date"]), ("2026-09-21", "2026-09-27"))
            with self.assertRaises(NeedsInput):
                date_window({"start_date": "2026-10-02"}, "Asia/Hong_Kong")

    def test_statistics_unknowns_not_zero(self):
        summary = summarize([match(1), match(2, player_slot=128, kills=None),
                             match(3, player_slot=None, kills=float("nan"))])
        self.assertEqual(summary["win_rate_percent"], 50)
        self.assertEqual(summary["unknown_outcomes"], 1)
        self.assertEqual(summary["average_kills"], 6)
        self.assertEqual(summary["metric_sample_counts"]["kills"], 1)


class StorageTests(TempTest):
    async def test_binding_scope_and_alias_uniqueness(self):
        s = self.service
        s.save_binding("group:a", "alice", 42, "A")
        s.save_binding("group:b", "alice", 43, "A")
        self.assertEqual(s._resolve("me", "group:a", "alice"), 42)
        self.assertEqual(s._resolve("A", "group:b", "other"), 43)
        with self.assertRaises(NeedsInput):
            s._resolve("me", "private:a", "alice")
        with self.assertRaises(ValueError):
            s.save_binding("group:a", "bob", 44, "a")
        s.remove_binding("group:a", "alice")
        self.assertEqual(s._resolve("me", "group:b", "alice"), 43)

    async def test_clear_cache_preserves_bindings_and_budget(self):
        self.store.bind("g", "u", 42, "")
        self.store.reserve_request("2026-10-30", 1)
        self.store.put_cache("k", {"value": 1}, 100, STAMP["fetched_at"])
        self.assertEqual(self.store.clear_cache()["removed"], 1)
        self.assertEqual(len(self.store.bindings()), 1)
        self.assertFalse(self.store.reserve_request("2026-10-30", 1))

    async def test_cache_expires_and_logs_bounded(self):
        self.store.put_cache("expired", [], -1, STAMP["fetched_at"])
        self.assertIsNone(self.store.cached("expired"))
        for _ in range(210):
            self.store.record_query("matches", "ok", 2)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM query_log").fetchone()[0], 200)

    async def test_shared_budget_and_daily_limit(self):
        settings = {**DEFAULTS, "requests_per_minute": 1}
        budget = RequestBudget(self.store, settings)
        budget.reserve()
        with self.assertRaises(DataError) as error:
            budget.reserve()
        self.assertEqual(error.exception.code, "rate_limited")
        settings["requests_per_minute"] = 30
        settings["daily_request_limit"] = 1
        with self.assertRaises(DataError) as error:
            budget.reserve()
        self.assertEqual(error.exception.code, "budget_exceeded")


class RoutingTests(TempTest):
    def fake(self, router, name, responses):
        fake = AsyncMock(side_effect=responses)
        router.clients[name].get = fake
        return fake

    async def test_initial_failure_falls_back_and_pins_pages(self):
        router = self.service.api
        router.settings["stratz_api_key"] = "test-token"
        od = self.fake(router, "opendota", [DataError("timeout", "offline")])
        st = self.fake(router, "stratz", [([match(1)], STAMP), ([], STAMP)])
        _, stamp = await router.get("/players/42/matches", {"offset": 0})
        self.assertEqual(stamp["source"], "STRATZ")
        await router.get("/players/42/matches", {"offset": 100})
        self.assertEqual((od.await_count, st.await_count), (1, 2))
        self.assertEqual(router.trace[1]["fallback"]["reason"], "timeout")

    async def test_empty_is_valid_and_later_page_failure_never_switches(self):
        router = self.service.api
        router.settings["stratz_api_key"] = "test-token"
        self.fake(router, "opendota", [([], STAMP), DataError("timeout", "offline")])
        st = self.fake(router, "stratz", [])
        rows, _ = await router.get("/players/42/matches")
        self.assertEqual(rows, [])
        with self.assertRaises(DataError):
            await router.get("/players/42/matches", {"offset": 100})
        st.assert_not_awaited()

    async def test_disabled_fallback_and_local_budget_failure(self):
        router = self.service.api
        router.settings.update(stratz_api_key="test-token", fallback_enabled=False)
        od = self.fake(router, "opendota", [DataError("timeout", "offline"), DataError("budget_exceeded", "limit")])
        st = self.fake(router, "stratz", [])
        with self.assertRaises(DataError):
            await router.get("/players/42")
        router.settings["fallback_enabled"] = True
        with self.assertRaises(DataError):
            await router.get("/players/42")
        self.assertEqual(od.await_count, 2)
        st.assert_not_awaited()

    async def test_dictionary_remains_opendota_and_stratz_priority(self):
        router = self.service.api
        router.settings["data_source_priority"] = "stratz"
        od = self.fake(router, "opendota", [(HEROES, STAMP), ([], STAMP)])
        st = self.fake(router, "stratz", [({"profile": {}}, STAMP)])
        await router.get("/heroes")
        await router.get("/heroStats")
        await router.get("/players/42")
        self.assertEqual((od.await_count, st.await_count), (2, 1))


class ServiceTests(TempTest):
    def source(self, rows_by_id):
        async def get(path, params=None, ttl=None):
            if path == "/heroes":
                return HEROES, STAMP
            aid = int(path.split("/")[2])
            rows = rows_by_id[aid]
            offset, size = params["offset"], params["limit"]
            return rows[offset:offset + size], STAMP
        self.service.api.clients["opendota"].get = AsyncMock(side_effect=get)

    async def test_filters_limit_and_hero_pool(self):
        self.source({42: [match(1, hero_id=1), match(2, player_slot=128), match(3), match(4)]})
        result = await self.service.query("matches", {"target": "42", "hero": "斧王", "outcome": "win",
                                         "mode": "ranked", "limit": 1, "period": "all"}, "g", "u")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["matches"][0]["match_id"], 3)
        self.assertEqual(result["meta"]["coverage"]["matched_rows"], 2)
        self.assertTrue(result["meta"]["coverage"]["result_truncated"])
        result = await self.service.query("hero_pool", {"target": "42", "period": "all"}, "g", "u")
        self.assertEqual(result["data"]["heroes"][0]["matches"], 3)

    async def test_common_matches_compare(self):
        self.source({42: [match(1), match(2)], 43: [match(2, player_slot=128), match(3)]})
        result = await self.service.query("compare", {"targets": ["42", "43"], "period": "all",
                                         "common_matches_only": True}, "g", "u")
        players = result["data"]["players"]
        self.assertEqual([p["match_ids"] for p in players], [[2], [2]])
        self.assertEqual([p["summary"]["win_rate_percent"] for p in players], [100, 0])

    async def test_scan_limit_marks_incomplete(self):
        self.service.update_settings({"scan_limit": 100})
        self.source({42: [match(i) for i in range(101)]})
        result = await self.service.query("matches", {"target": "42", "period": "all"}, "g", "u")
        self.assertEqual(result["meta"]["coverage"]["scanned_rows"], 100)
        self.assertFalse(result["meta"]["coverage"]["available_window_complete"])

    async def test_missing_time_never_claims_complete(self):
        self.source({42: [match(1, start_time=None)]})
        result = await self.service.query("matches", {"target": "42", "period": "all"}, "g", "u")
        self.assertFalse(result["meta"]["coverage"]["available_window_complete"])
        self.assertEqual(result["meta"]["coverage"]["missing_time_rows"], 1)

    async def test_invalid_params_and_unknown_self_do_not_request_data(self):
        fake = self.service.api.clients["opendota"].get = AsyncMock()
        for params in ({"sql": "SELECT 1"}, {"target": "me"}, {"target": "42", "player": "43"}):
            result = await self.service.query("profile", params, "g", "u")
            self.assertEqual(result["status"], "needs_input")
        fake.assert_not_awaited()

    async def test_busy_query_does_not_leak_other_trace(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def get(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"profile": {"personaname": "sample"}}, STAMP
        self.service.api.clients["opendota"].get = get
        task = asyncio.create_task(self.service.query("profile", {"target": "42"}, "g", "u"))
        await entered.wait()
        result = await self.service.query("profile", {"target": "43"}, "g", "u")
        self.assertEqual(result["code"], "busy")
        self.assertEqual(result["meta"]["source_requests"], [])
        release.set()
        self.assertEqual((await task)["status"], "ok")

    async def test_stratz_survives_dictionary_failure_and_keeps_id_namespaces(self):
        self.service.update_settings({"data_source_priority": "stratz", "stratz_api_key": "test-token"})
        self.service.api.clients["opendota"].get = AsyncMock(side_effect=DataError("timeout", "offline"))
        row = Stratz._match({"match": stratz_match()}, 123)
        self.service.api.clients["stratz"].get = AsyncMock(return_value=(row, {**STAMP, "source": "STRATZ"}))
        result = await self.service.query("match", {"match_id": "123"}, "g", "u")
        self.assertEqual(result["status"], "ok")
        player = result["data"]["players"][0]
        self.assertIsNone(player["player_slot"])
        self.assertEqual(player["stratz_player_slot"], 5)
        self.assertFalse(player["won"])
        self.assertIsNone(result["data"]["patch_id"])
        self.assertEqual(result["data"]["stratz_game_version_id"], 999)
        self.assertTrue(result["meta"]["warnings"])

    async def test_shutdown_disables_future_queries(self):
        await self.service.close()
        result = await self.service.query("profile", {"target": "42"}, "g", "u")
        self.assertEqual(result["code"], "disabled")


class AdapterTests(TempTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.calls = []
        self.status = 200
        self.payload = []
        async def handler(request):
            self.calls.append({"path": request.path, "query": dict(request.query),
                               "project": request.query.getall("project", []),
                               "auth": request.headers.get("Authorization"),
                               "body": await request.json() if request.method == "POST" else None})
            return web.json_response(self.payload, status=self.status)
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.patches = [patch.object(od_module, "BASE_URL", f"http://127.0.0.1:{port}"),
                        patch.object(st_module, "ENDPOINT", f"http://127.0.0.1:{port}/graphql")]
        for p in self.patches:
            p.start()
        self.service.update_settings({"stratz_api_key": "offline-stratz-token", "opendota_api_key": "offline-od-token"})

    async def asyncTearDown(self):
        await super().asyncTearDown()
        await self.runner.cleanup()
        for p in self.patches:
            p.stop()

    async def test_opendota_cache_and_key_never_persisted(self):
        client = self.service.api.clients["opendota"]
        self.payload = {"profile": {"personaname": "fixture"}}
        _, first = await client.get("/players/42")
        _, second = await client.get("/players/42")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["query"]["api_key"], "offline-od-token")
        with self.store.connect() as db:
            self.assertNotIn("offline-od-token", repr([tuple(row) for row in db.execute("SELECT * FROM cache")]))

    async def test_http_failures_are_not_empty_results(self):
        client = self.service.api.clients["opendota"]
        for status, code in ((401, "upstream_access_denied"), (429, "upstream_rate_limited"),
                             (404, "not_found"), (503, "upstream_error")):
            self.status = status
            with self.subTest(status=status), self.assertRaises(DataError) as error:
                await client.get("/players/42")
            self.assertEqual(error.exception.code, code)

    async def test_opendota_projection_retains_filters_and_statistic_fields(self):
        await self.service.api.clients["opendota"].get("/players/42/matches", {"limit": 5, "hero_id": 2})
        projected = self.calls[0]["project"]
        for field in ("hero_id", "start_time", "game_mode", "kills", "gold_per_min", "xp_per_min", "last_hits"):
            self.assertIn(field, projected)
        self.assertEqual(self.calls[0]["query"]["hero_id"], "2")

    async def test_stratz_graphql_and_response_normalization(self):
        self.payload = {"data": {"player": {"matches": [stratz_match()]}}}
        client = self.service.api.clients["stratz"]
        rows, stamp = await client.get("/players/42/matches", {"limit": 10, "offset": 20, "hero_id": 2, "win": 0})
        self.assertEqual(rows[0]["kills"], 3)
        self.assertEqual(stamp["source"], "STRATZ")
        request = self.calls[0]["body"]["variables"]["request"]
        self.assertEqual(request, {"take": 10, "skip": 20, "orderBy": "DESC", "heroIds": [2], "isVictory": False})
        self.assertEqual(self.calls[0]["auth"], "Bearer offline-stratz-token")
        self.assertNotIn("mutation", self.calls[0]["body"]["query"].lower())

    async def test_stratz_errors_partial_data_and_missing_token(self):
        client = self.service.api.clients["stratz"]
        self.payload = {"errors": [{"message": "private diagnostic"}], "data": {"player": {"matches": []}}}
        with self.assertRaises(DataError) as error:
            await client.get("/players/42/matches")
        self.assertEqual(error.exception.code, "schema_error")
        self.assertNotIn("private diagnostic", error.exception.message)
        self.service.update_settings({"stratz_api_key": ""})
        with self.assertRaises(DataError) as error:
            await client.get("/players/42/matches")
        self.assertEqual(error.exception.code, "missing_key")
        self.assertEqual(len(self.calls), 1)

    async def test_stratz_rejects_wrong_player_outcome_and_order(self):
        for kind in ("wrong_player", "conflicting_result", "ascending", "duplicate"):
            row = stratz_match()
            rows = [row]
            if kind == "wrong_player":
                row["players"][0]["steamAccountId"] = 43
            elif kind == "conflicting_result":
                row["players"][0]["isVictory"] = True
            elif kind == "ascending":
                later = stratz_match(124)
                later["startDateTime"] += 1
                rows.append(later)
            else:
                rows.append(stratz_match())
            with self.subTest(kind=kind), self.assertRaises(DataError):
                Stratz._matches({"player": {"matches": rows}}, 42, {"limit": 10})

    async def test_sources_share_allowance_but_not_cache(self):
        self.service.update_settings({"daily_request_limit": 1})
        self.payload = []
        await self.service.api.clients["opendota"].get("/players/42/matches")
        with self.assertRaises(DataError) as error:
            await self.service.api.clients["stratz"].get("/players/42/matches")
        self.assertEqual(error.exception.code, "budget_exceeded")
        self.assertEqual(len(self.calls), 1)
