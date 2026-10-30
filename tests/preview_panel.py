"""Isolated localhost UI harness using real AstrBot handlers and bridge SDK.

Only fixture data and a temporary config are used. This is not a production
Dashboard authentication test and must never be exposed on a public interface.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from scratch import TestDirectory

parser = argparse.ArgumentParser()
parser.add_argument("--astrbot-app", required=True, type=Path)
parser.add_argument("--port", type=int, default=18765)
args = parser.parse_args()
repo = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(args.astrbot_app.resolve()), str(repo.parent)]
runtime = TestDirectory("panel-")
os.environ["ASTRBOT_ROOT"] = runtime.name
sys.dont_write_bytecode = True

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, Response
from astrbot.api.star import Context, StarTools
from astrbot.api.web import PluginRequest, bind_request_context
from astrbot_plugin_dota2_data.main import Dota2DataPlugin
from test_framework import Config
from test_data import match, HEROES, STAMP

app = FastAPI()
context = object.__new__(Context)
context.registered_web_apis = []
with patch.object(StarTools, "get_data_dir", return_value=Path(runtime.name) / "plugin-data"):
    plugin = Dota2DataPlugin(context, Config(Path(runtime.name) / "config.json"))


async def fixture_get(path, params=None, ttl=None):
    if path == "/heroes":
        return HEROES, STAMP
    if path.endswith("/matches"):
        return [match(123), match(124, player_slot=128)], STAMP
    return {"profile": {"personaname": "UI test player <script>not code</script>"}}, STAMP


plugin.service.api.clients["opendota"].get = fixture_get

PARENT = '''<!doctype html><html><head><meta charset="utf-8"><title>Dota2 隔离面板验收</title>
<style>body{margin:0;background:#101820;color:white;font:14px sans-serif}header{padding:8px}iframe{width:100%;height:calc(100vh - 36px);border:0}</style></head>
<body><header>隔离验收 · 样本数据 · 不连接正式 Bot</header><iframe id="panel" src="/panel/index.html"></iframe>
<script>
window.addEventListener('message', async e => {
 const frame=document.getElementById('panel');const m=e.data;
 if(e.origin!==location.origin||e.source!==frame.contentWindow||m?.channel!=='astrbot-plugin-page')return;
 const send=x=>frame.contentWindow.postMessage({channel:'astrbot-plugin-page',...x},location.origin);
 if(m.kind==='ready')return send({kind:'context',context:{locale:'zh-CN',isDark:true}});
 if(m.kind!=='request')return;
 try {
  if(!['api:get','api:post'].includes(m.action))throw Error('Unsupported test operation');
  const r=await fetch('/test-api/'+m.endpoint,{method:m.action==='api:get'?'GET':'POST',headers:{'Content-Type':'application/json','X-Test-Identity':'isolated-admin'},...(m.action==='api:post'?{body:JSON.stringify(m.body||{})}:{})});
  let data=await r.json();if(!r.ok||data.status==='error')throw Error(data.message||'Request failed');
  if(data.status==='ok'&&Object.hasOwn(data,'data'))data=data.data;
  send({kind:'response',requestId:m.requestId,ok:true,data});
 }catch(err){send({kind:'response',requestId:m.requestId,ok:false,error:String(err.message)});}
});</script></body></html>'''


@app.get("/")
async def parent():
    return HTMLResponse(PARENT)


@app.get("/bridge.js")
async def bridge():
    return FileResponse(args.astrbot_app / "astrbot/dashboard/plugin_page_bridge.js", media_type="application/javascript")


@app.get("/panel/{name}")
async def assets(name: str):
    if name not in {"index.html", "app.js", "style.css"}:
        return Response(status_code=404)
    path = repo / "pages/dashboard" / name
    if name == "index.html":
        content = path.read_text(encoding="utf-8").replace("</body>", '<script src="/bridge.js"></script></body>')
        return HTMLResponse(content)
    return FileResponse(path)


@app.api_route("/test-api/{endpoint:path}", methods=["GET", "POST"])
async def api(endpoint: str, request: Request):
    for path, handler, methods, _ in context.registered_web_apis:
        if path == "/astrbot_plugin_dota2_data/" + endpoint and request.method in methods:
            identity = "isolated-admin" if request.headers.get("X-Test-Identity") == "isolated-admin" else None
            with bind_request_context(PluginRequest(request, username=identity)):
                return await handler()
    return Response(status_code=404)


if __name__ == "__main__":
    import uvicorn
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
    finally:
        asyncio.run(plugin.terminate())
