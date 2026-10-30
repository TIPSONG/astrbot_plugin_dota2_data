"""Integration with the real installed framework, without starting a Bot."""
import asyncio
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from scratch import TestDirectory

HAS_ASTRBOT = importlib.util.find_spec("astrbot") is not None
if HAS_ASTRBOT:
    from astrbot.api.star import Context, StarTools
    from astrbot.api.web import PluginRequest, bind_request_context
    from astrbot.core.provider.register import llm_tools
    from astrbot.core.star.star_handler import star_handlers_registry, EventType
    from starlette.requests import Request
    from astrbot_plugin_dota2_data.main import Dota2DataPlugin, PLUGIN_NAME
from astrbot_plugin_dota2_data.settings import DEFAULTS


class Config(dict):
    def __init__(self, path):
        self.path = path
        super().__init__(json.loads(path.read_text()) if path.exists() else DEFAULTS)

    def save_config(self):
        self.path.write_text(json.dumps(self), encoding="utf-8")


def event(message, mid="1", uid="user", scope="test:group"):
    # No send, request_llm, or persona APIs: accidental calls fail the test.
    return SimpleNamespace(message_str=message, unified_msg_origin=scope,
                           message_obj=SimpleNamespace(message_id=mid), get_sender_id=lambda: uid)


@unittest.skipUnless(HAS_ASTRBOT, "Pass --astrbot-app to validate the real framework")
class FrameworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TestDirectory()
        self.base = Path(self.tmp.name)
        self.context = object.__new__(Context)
        self.context.registered_web_apis = []
        self.data_patch = patch.object(StarTools, "get_data_dir", return_value=self.base / "plugin-data")
        self.data_patch.start()
        self.config = Config(self.base / "plugin.json")
        self.plugin = Dota2DataPlugin(self.context, self.config)
        self.sentinel = self.base / "flora-persona.json"
        self.sentinel.write_text('{"persona":"sentinel-original-persona"}', encoding="utf-8")
        self.persona_before = self.sentinel.read_bytes()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.assertEqual(self.sentinel.read_bytes(), self.persona_before)
        self.data_patch.stop()
        self.tmp.cleanup()

    async def call(self, name, body=None, username="test-admin"):
        data = json.dumps(body).encode() if body is not None else b""
        async def receive():
            return {"type": "http.request", "body": data, "more_body": False}
        raw = Request({"type": "http", "method": "POST" if body is not None else "GET",
                       "path": "/test", "query_string": b"", "headers": [],
                       "scheme": "http", "server": ("127.0.0.1", 0)}, receive)
        with bind_request_context(PluginRequest(raw, username=username, plugin_name=PLUGIN_NAME)):
            response = await getattr(self.plugin, name)()
        return response.status_code, json.loads(response.body)

    async def test_real_registration_only_native_tool_hooks(self):
        handlers = star_handlers_registry.get_handlers_by_module_name(Dota2DataPlugin.__module__)
        self.assertEqual(len(handlers), 2)
        self.assertTrue(all(h.event_type == EventType.OnCallingFuncToolEvent for h in handlers))
        query = next((tool for tool in llm_tools.func_list if tool.name == "dota2_query"), None)
        self.assertIsNotNone(query)
        self.assertEqual(query.parameters["properties"]["parameters"]["type"], "object")
        self.assertEqual(len(self.context.registered_web_apis), 9)

    async def test_real_framework_discovers_and_rewrites_panel(self):
        from astrbot.dashboard.services.plugin_page_service import PluginPageService
        from astrbot.core.star.star import StarMetadata
        root = Path(__file__).resolve().parents[1]
        manager = SimpleNamespace(plugin_store_path=str(root.parent), reserved_plugin_path=str(root.parent))
        pages = PluginPageService(manager)
        metadata = StarMetadata(name=PLUGIN_NAME, root_dir_name=root.name, reserved=False)
        found = await pages.discover_plugin_pages(metadata)
        self.assertEqual([page.name for page in found], ["dashboard"])
        path = await pages.resolve_plugin_page_file(metadata, "dashboard", "index.html")
        rewritten = pages.rewrite_plugin_page_html(path.read_text(encoding="utf-8"), PLUGIN_NAME,
                                                   "dashboard", "index.html", theme="dark")
        self.assertIn("/api/plugin/page/bridge-sdk.js", rewritten)
        self.assertIn("app.js", rewritten)

    async def test_all_admin_routes_require_identity(self):
        for name in ("page_status", "page_settings", "page_save_settings", "page_bindings",
                     "page_save_binding", "page_remove_binding", "page_queries", "page_query", "page_clear_cache"):
            with self.subTest(name=name):
                status, _ = await self.call(name, {}, username=None)
                self.assertEqual(status, 401)

    async def test_settings_save_reload_and_secret_redaction(self):
        status, _ = await self.call("page_save_settings", {"stratz_api_key": "fixture-stratz-secret",
            "opendota_api_key": "fixture-opendota-secret", "data_source_priority": "stratz", "timeout_seconds": 17})
        self.assertEqual(status, 200)
        status, body = await self.call("page_settings")
        self.assertNotIn("fixture-", json.dumps(body))
        self.assertTrue(body["data"]["stratz_api_key_configured"])
        await self.call("page_save_settings", {"stratz_api_key": "", "opendota_api_key": ""})
        self.assertEqual(self.plugin.settings["stratz_api_key"], "fixture-stratz-secret")
        await self.plugin.terminate()
        self.plugin = Dota2DataPlugin(self.context, Config(self.config.path))
        self.assertEqual(self.plugin.settings["timeout_seconds"], 17)
        self.assertEqual(self.plugin.service.api.settings["data_source_priority"], "stratz")
        status, _ = await self.call("page_save_settings", {"clear_stratz_api_key": True})
        self.assertEqual(status, 200)
        self.assertEqual(self.plugin.settings["stratz_api_key"], "")
        self.assertNotIn("fixture-", repr(self.plugin.service.recent_queries()))

    async def test_settings_reject_conflicts_invalid_values_and_active_query(self):
        for body in ({"clear_stratz_api_key": True, "stratz_api_key": "new"},
                     {"max_matches": 1000}, {"clear_api_key": "true"}, {"unknown_setting": 1}):
            self.assertEqual((await self.call("page_save_settings", body))[0], 400)
        async with self.plugin.service._query_lock:
            self.assertEqual((await self.call("page_save_settings", {"timeout_seconds": 20}))[0], 400)

    async def test_settings_disk_failure_preserves_active_values(self):
        with patch.object(self.config, "save_config", side_effect=OSError("fixture-secret")):
            status, body = await self.call("page_save_settings", {"timeout_seconds": 20})
        self.assertEqual(status, 500)
        self.assertNotIn("fixture-secret", json.dumps(body))
        self.assertEqual(self.plugin.settings["timeout_seconds"], 15)
        self.assertEqual(self.config["timeout_seconds"], 15)

    async def test_bind_requires_later_confirmation_same_sender_and_scope(self):
        p = self.plugin
        first = event("绑定我的账号42")
        result = json.loads(await p.dota2_manage_binding(first, "bind", "42", "tester"))
        self.assertEqual(result["status"], "needs_confirmation")
        for msg in (event("确认", "1"), event("确认", "2", "other"), event("确认", "2", scope="other"), event("随便", "2")):
            result = json.loads(await p.dota2_manage_binding(msg, "bind", confirm=True))
            self.assertEqual(result["status"], "needs_input")
        result = json.loads(await p.dota2_manage_binding(event("确认", "2"), "bind", "999", confirm=True))
        self.assertEqual(result["data"]["account_id"], 42)
        self.assertEqual(len(p.service.bindings()), 1)

    async def test_binding_expiry_and_unbind(self):
        p = self.plugin
        await p.dota2_manage_binding(event("绑定账号42"), "bind", "42")
        for pending in p._pending.values():
            pending["expires"] = 0
        result = json.loads(await p.dota2_manage_binding(event("确认", "2"), "bind", confirm=True))
        self.assertEqual(result["status"], "needs_input")
        p.service.save_binding("test:group", "user", 42)
        await p.dota2_manage_binding(event("解绑账号", "3"), "unbind")
        result = json.loads(await p.dota2_manage_binding(event("确认", "4"), "unbind", confirm=True))
        self.assertTrue(result["data"]["removed"])

    async def test_panel_binding_crud_and_query_metadata(self):
        status, body = await self.call("page_save_binding", {"scope": "g", "user_id": "u", "account_id": "42", "alias": "sample"})
        self.assertEqual(status, 200)
        self.assertEqual((await self.call("page_bindings"))[1]["data"][0]["account_id"], 42)
        self.assertTrue((await self.call("page_remove_binding", {"scope": "g", "user_id": "u"}))[1]["data"]["removed"])
        expected = {"status": "ok", "data": {"sample": 1}, "meta": {"source": "STRATZ"}}
        self.plugin.service.query = AsyncMock(return_value=expected)
        self.assertEqual((await self.call("page_query", {"action": "profile", "params": {}}))[1], {"result": expected})

    async def test_tool_returns_data_without_event_send_or_persona_access(self):
        self.plugin.service.query = AsyncMock(return_value={"status": "empty", "data": None})
        result = json.loads(await self.plugin.dota2_query(event("查一下我的战绩"), "matches", {}))
        self.assertEqual(result["status"], "empty")
        self.plugin.service.query.assert_awaited_once_with("matches", {}, "test:group", "user")

    async def test_reload_cleanup_preserves_new_routes(self):
        replacement = Dota2DataPlugin(self.context, self.config)
        try:
            await self.plugin.terminate()
            self.assertEqual(len(self.context.registered_web_apis), 9)
            self.assertTrue(all(row[1].__self__ is replacement for row in self.context.registered_web_apis))
        finally:
            await replacement.terminate()
        self.assertEqual(self.context.registered_web_apis, [])
