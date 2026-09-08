"""诊断：WS 端到端模拟「连续两句话」，验证 agent_go_result 回执与线程一致性。"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 跳过真实 Playwright MCP 连接（诊断只需验证 agent 结束标志链路）
import get_job_agent.agent.browser_mcp as _bm

async def _noop_init():
    _bm._tools_cache = []
    return []

async def _noop_close():
    pass

_bm.init_browser_mcp_tools = _noop_init
_bm.close_browser_mcp = _noop_close

from fastapi.testclient import TestClient

from get_job_agent.main import app


async def main() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"from": "panel", "type": "register", "client_id": "diag-client"}))
            got = json.loads(ws.receive_text())
            print("register ->", got.get("type"))

            for i, goal in enumerate(["你好，请用一句话介绍你自己，不要调用工具。",
                                      "好的，谢谢。再说说你平时能帮我做什么？"]):
                print(f"\n===== 第 {i+1} 句: {goal[:20]}... =====")
                ws.send_text(json.dumps({
                    "from": "panel", "type": "agent_go",
                    "idem": f"diag-{i}",
                    "payload": {"goal": goal, "mode": "unattended", "max_greetings": 20},
                }))
                while True:
                    raw = ws.receive_text()
                    msg = json.loads(raw)
                    t = msg.get("type")
                    if t == "agent_event":
                        kind = msg.get("payload", {}).get("kind")
                        if kind in ("assistant", "interrupt"):
                            print(f"  event[{kind}]:", (msg.get("payload", {}).get("text") or "")[:60])
                    elif t == "agent_status":
                        pass
                    elif t == "agent_go_result":
                        print("  result:", {k: msg.get("payload", {}).get(k) for k in ("ok", "output", "error", "stopped")})
                        break
                    elif t == "page_connected":
                        pass
                    else:
                        print("  other:", t)


if __name__ == "__main__":
    asyncio.run(main())
