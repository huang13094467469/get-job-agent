"""本地启动 Local Agent Server（等价于 scripts/dev.sh，跨平台）。

用法：
    python main.py                 # 默认 127.0.0.1:8791，带 --reload
    python main.py --no-reload
    python main.py --host 0.0.0.0 --port 8791

也可用环境变量覆盖：SERVER_HOST / SERVER_PORT。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# psycopg3（AsyncPostgresSaver）异步实现不支持 Windows 默认的 ProactorEventLoop，须在事件循环创建前
# 切到 SelectorEventLoop（P0-1 持久 checkpointer 前提）。放模块级：既覆盖 `python main.py`
# 主进程，也覆盖 uvicorn --reload 在 Windows 以 spawn 启动子进程时对本 __main__ 模块的重新导入。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ROOT = Path(__file__).resolve().parent
SRC_DIR = ROOT / "src"


def _prepare_env() -> None:
    """切到项目根目录并把 src 加入导入路径（对齐 dev.sh 的 cd + PYTHONPATH）。"""
    # config.yaml（config/）、.env、简历目录（jianli）都相对项目根 cwd 解析
    os.chdir(ROOT)
    src = str(SRC_DIR)
    if src not in sys.path:
        sys.path.insert(0, src)
    # 传给 uvicorn --reload 派生的子进程，确保能 import get_job_agent
    existing = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = src if not existing else f"{src}{os.pathsep}{existing}"


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 Get-Job Agent Local Server")
    parser.add_argument("--host", default=os.environ.get("SERVER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SERVER_PORT", "8791")))
    parser.add_argument(
        "--reload",
        dest="reload",
        action="store_true",
        default=True,
        help="开发热重载（默认开启）",
    )
    parser.add_argument(
        "--no-reload",
        dest="reload",
        action="store_false",
        help="关闭热重载",
    )
    args = parser.parse_args()

    _prepare_env()

    import uvicorn

    uvicorn.run("get_job_agent.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
