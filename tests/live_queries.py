"""Explicit, bounded public API smoke test; never reads existing Bot config."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from scratch import TestDirectory

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo.parent))
from astrbot_plugin_dota2_data.dota_data.service import DataService
from astrbot_plugin_dota2_data.settings import DEFAULTS

parser = argparse.ArgumentParser()
parser.add_argument("--stratz-token-file", type=Path)
args = parser.parse_args()


async def run():
    settings = {**DEFAULTS, "max_matches": 5, "scan_limit": 100, "daily_request_limit": 20}
    if args.stratz_token_file:
        settings.update(stratz_api_key=args.stratz_token_file.read_text(encoding="utf-8-sig").strip(),
                        data_source_priority="stratz", fallback_enabled=False)
    with TestDirectory("live-") as folder:
        service = DataService(Path(folder), settings)
        report = {"source": settings["data_source_priority"], "results": []}
        try:
            # Discover publicly listed professional accounts, not the user's own account.
            pros, _ = await service.api.clients["opendota"].get("/proPlayers")
            ids = list(dict.fromkeys(row["account_id"] for row in pros
                                    if isinstance(row, dict) and type(row.get("account_id")) is int))[:2]
            if len(ids) < 2:
                raise RuntimeError("Public player discovery returned too few accounts")
            tasks = [("profile", {"target": str(ids[0])}),
                     ("matches", {"target": str(ids[0]), "period": "all", "limit": 5}),
                     ("hero_pool", {"target": str(ids[0]), "period": "all", "limit": 5}),
                     ("compare", {"targets": list(map(str, ids)), "period": "all", "limit": 5}),
                     ("meta", {"limit": 5})]
            for action, params in tasks:
                result = await service.query(action, params, "smoke-test", "test-runner")
                report["results"].append({"action": action, "status": result["status"],
                    "code": result.get("code"), "source": result["meta"].get("source"),
                    "coverage": result["meta"].get("coverage"),
                    "summary": (result.get("data") or {}).get("summary")})
                if action == "matches" and result["status"] == "ok":
                    if "hero" not in params:
                        first = result["data"]["matches"][0]
                        tasks.append(("matches", {"target": str(ids[0]), "period": "all", "limit": 5,
                            "hero": str(first["hero_id"]), "outcome": "win" if first["won"] else "loss"}))
                    else:
                        report["results"][-1]["filters"] = {"hero": params["hero"], "outcome": params["outcome"]}
                        report["results"][-1]["filters_verified"] = all(
                            row["hero_id"] == int(params["hero"]) and row["won"] == (params["outcome"] == "win")
                            for row in result["data"]["matches"])
                        continue
                    mid = result["data"]["matches"][0]["match_id"]
                    single = await service.query("match", {"match_id": str(mid)}, "smoke-test", "test-runner")
                    report["results"].append({"action": "match", "status": single["status"],
                        "code": single.get("code"), "source": single["meta"].get("source"),
                        "players": len((single.get("data") or {}).get("players", [])),
                        "parsed": single["meta"].get("parsed")})
            report["requests"] = service.status()["requests_today"]
        except Exception as exc:
            report["error"] = {"type": type(exc).__name__, "code": getattr(exc, "code", None)}
        finally:
            await service.close()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return "error" not in report and all(row["status"] in {"ok", "empty"} for row in report["results"])


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(run()) else 1)
