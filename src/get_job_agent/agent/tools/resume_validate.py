"""简历校验：检测关键字段缺失并给出补充提示。"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

# 关键字段缺失检测清单：字段路径 -> 提示文案
REQUIRED_FIELDS: dict[str, str] = {
    "name": "缺少姓名",
    "phone": "缺少手机号",
    "education": "缺少学历信息",
    "skills": "缺少技能栈",
    "intent.position": "缺少求职意向岗位",
}


def _get_path(data: dict[str, Any], path: str) -> Any:
    """按点路径取嵌套字段。"""
    cur: Any = data
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    # 空 list/str/null 视为缺失
    if cur is None or cur == "" or (isinstance(cur, list) and not cur):
        return None
    return cur


@tool
def validate_resume(profile_json: str) -> str:
    """校验结构化画像，返回缺失项清单（JSON 字符串）。

    参数 profile_json 为结构化抽取结果 JSON 字符串。返回值形如
    {"missing": [{"field": "name", "hint": "缺少姓名"}, ...], "complete": bool}
    """
    try:
        data: dict[str, Any] = json.loads(profile_json)
    except json.JSONDecodeError:
        return json.dumps(
            {"missing": [{"field": "_raw", "hint": "画像不是合法 JSON"}], "complete": False},
            ensure_ascii=False,
        )

    missing = [
        {"field": field, "hint": hint}
        for field, hint in REQUIRED_FIELDS.items()
        if _get_path(data, field) is None
    ]
    return json.dumps({"missing": missing, "complete": not missing}, ensure_ascii=False)


__all__ = ["validate_resume", "REQUIRED_FIELDS"]