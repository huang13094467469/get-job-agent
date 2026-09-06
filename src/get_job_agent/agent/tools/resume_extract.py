"""简历结构化抽取：用对话 LLM(Qwen) 把简历文本抽取为结构化 JSON。"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from ...core.config import get_settings
from ...core.logs import logger
from ..harness import build_model

EXTRACT_PROMPT = """你是简历信息抽取器。从下面的简历文本中抽取结构化字段，只返回 JSON（不要多余文字）。

字段结构（缺失则用 null，不要编造，不要省略键）：
{
  "name": "姓名",
  "phone": "手机号",
  "email": "邮箱",
  "intent": {"position": "求职意向岗位", "salary_min": 最小期望月薪(数字), "salary_max": 最大期望月薪(数字)},
  "years": "工作年限(数字，应届填0)",
  "education": "最高学历，如 本科/硕士/博士",
  "skills": ["技能栈数组"],
  "experiences": [{"company": "公司", "title": "职位", "start": "YYYY-MM", "end": "YYYY-MM", "summary": "职责与成果"]}],
  "projects": [{"name": "项目名", "role": "角色", "summary": "技术栈与成果"}]
}

简历文本：
{text}
"""


@tool
def extract_resume(resume_text: str) -> str:
    """把简历纯文本结构化为 JSON（姓名/联系方式/意向/经历/项目/技能/学历）。

    参数 resume_text 为已抽取的简历纯文本。返回 JSON 字符串。
    """
    settings = get_settings()
    model = build_model(settings)
    prompt = EXTRACT_PROMPT.replace("{text}", resume_text[:12000])
    resp = model.invoke(prompt)
    content = resp.content.strip()

    # 容忍模型在代码块里输出 JSON
    if content.startswith("```"):
        content = content.split("```", 2)[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip("` \n")

    try:
        parsed: dict[str, Any] = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.warning("extract_resume 未返回合法 JSON，原样包裹返回: {}", exc)
        parsed = {"_raw": content}

    return json.dumps(parsed, ensure_ascii=False)


__all__ = ["extract_resume"]