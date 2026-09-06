"""岗位「过目清单」账本：跨 thread、跨重启持久，避免反复读/比对/沟通同一岗位。

键 = 归一化的「公司|岗位名称」。按 user_key 分账（本项目固定 "default"）。
结果枚举建议：已打招呼 / 不匹配跳过 / 已沟通重复 / 发送失败。
文件落盘项目根 data/job_ledger.json（进程内加锁，写时原子替换）。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from ..core.logs import logger

USER_KEY = "default"

# 终态结果：只有这些才算“已处理”→ 列表快照过滤 + compare 短路跳过。
# 非终态（达标待沟通/信息不足）仅用于记录活动，不会阻止重新处理。
TERMINAL_RESULTS = {"已打招呼", "不匹配跳过", "已沟通重复"}

_LOCK = threading.Lock()
_DATA_DIR = Path(__file__).resolve().parents[3] / "data"   # 项目根/data
_FILE = _DATA_DIR / "job_ledger.json"


def _norm(s: Any) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def make_key(company: Any, position: Any) -> str:
    return f"{_norm(company)}|{_norm(position)}"


def _load() -> dict[str, Any]:
    if not _FILE.exists():
        return {}
    try:
        return json.loads(_FILE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save(data: dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_FILE)


def is_terminal(entry: dict[str, Any] | None) -> bool:
    """该过目记录是否为终态（据此决定 compare 是否短路、快照是否过滤）。"""
    return bool(entry and entry.get("result") in TERMINAL_RESULTS)


def record(user_key: str, company: Any, position: Any, result: str,
           reason: str = "", link: str | None = None) -> str:
    """登记/更新一条过目记录（覆盖同键）。返回该键。"""
    key = make_key(company, position)
    with _LOCK:
        data = _load()
        bucket = data.setdefault(user_key or USER_KEY, {})
        prev = bucket.get(key, {})
        bucket[key] = {
            "company": str(company or "").strip(), "position": str(position or "").strip(),
            "result": result, "reason": str(reason or ""),
            "link": link or prev.get("link"), "ts": time.time(),
        }
        _save(data)
    logger.info("job_ledger 写入 {} → {}（{}）_FILE={}", key, result, reason, _FILE)
    return key


def get_entry(user_key: str, company: Any, position: Any) -> dict[str, Any] | None:
    return _load().get(user_key or USER_KEY, {}).get(make_key(company, position))


def list_reviewed(user_key: str = USER_KEY) -> list[dict[str, Any]]:
    """按登记时间排序返回本账号已过目的岗位记录。"""
    rows = list(_load().get(user_key or USER_KEY, {}).values())
    rows.sort(key=lambda r: r.get("ts", 0))
    return rows


def clear(user_key: str | None = None) -> None:
    """清空过目清单（user_key=None 清全部）。用于换求职目标/重新开始。"""
    with _LOCK:
        data = _load()
        if user_key is None:
            data = {}
        else:
            data.pop(user_key, None)
        _save(data)


__all__ = [
    "USER_KEY", "TERMINAL_RESULTS", "make_key", "record",
    "get_entry", "is_terminal", "list_reviewed", "clear",
]
