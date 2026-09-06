"""T3.5 过滤引擎：按 config.yaml filters/matching 规则对岗位列表打分过滤。

输入：岗位列表（CS 采集的 jobs[]，字段含 name/company/salary/area/
      experience/education/tags/link）。
输出：带 match 分数、按分数降序的过滤结果；命中 threshold 的视为「可沟通」。
纯函数模块，便于单测；规则缺失时仅做硬性筛选并中性排序。
"""

from __future__ import annotations

import re
from typing import Any

from ..core.config import load_yaml_config

__all__ = ["filter_jobs"]


# 硬性条件：任一字段命中则排除该岗位
# 城市 / 经验 / 学历 / 薪资 逐项抽取关键字
def _extract_keywords(values: list[str]) -> set[str]:
    kw: set[str] = set()
    for v in values:
        if not v:
            continue
        for piece in re.split(r"[，,、/\\|；;\s]+", str(v)):
            piece = piece.strip()
            if piece:
                kw.add(piece)
    return kw


def _salary_annual_upper(salary_str: str) -> float:
    """把 Boss 薪资文本粗估为「年薪上限（万元）」，无法解析返回 inf。"""
    text = str(salary_str or "").replace("·", "").replace("薪", "")
    nums = re.findall(r"\d+", text)
    if not nums:
        return float("inf")
    hi = max(float(n) for n in nums)
    # 单位：K=千/月 → 年薪万元（乘 12 个月）
    if "k" in text.lower():
        return hi / 10.0 * 12
    if "万" in text:  # 「15-25万」Boss 上通常已是年薪
        return hi
    return hi  # 直接按「万」处理（薪资范围比较用上限）


def _hit(candidate_vals: list[Any], rules: list[str]) -> bool:
    """候选文本中出现任一规则关键字即命中。"""
    if not rules:
        return True  # 无规则 = 不限制
    pieces: list[str] = []
    for v in candidate_vals:
        if isinstance(v, (list, tuple, set)):
            pieces.extend(str(x) for x in v)
        else:
            pieces.append(str(v))
    joined = " ".join(pieces)
    for kw in rules:
        if kw and kw in joined:
            return True
    return False


def filter_jobs(
    jobs: list[dict[str, Any]],
    *,
    config_path: str = "config/config.yaml",
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """过滤并打分。

    jobs     : 岗位列表（可先经 CS 采集去重）。
    config_path / rules：二选一；不传 rules 时按 config_path 的 YAML 读取。
    """
    cfg = rules if rules is not None else (load_yaml_config(config_path) or {})
    filters: dict[str, list[str]] = cfg.get("filters", {}) or {}
    matching: dict[str, Any] = cfg.get("matching", {}) or {}
    threshold: float = float(matching.get("threshold", 0.6))
    weights: dict[str, float] = matching.get("weights", {}) or {}

    city_rules = filters.get("city", []) or []
    exp_rules = filters.get("experience", []) or []
    edu_rules = filters.get("education", []) or []
    salary_rules = filters.get("salary", []) or []
    # 薪资规则解析出「可接受年薪上限」；无规则视为不限制
    salary_caps = [_salary_annual_upper(r) for r in salary_rules]
    salary_caps = [c for c in salary_caps if c != float("inf")]
    max_salary_cap = max(salary_caps) if salary_caps else float("inf")

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for j in jobs or []:
        area = " ".join([str(j.get("area") or ""), " ".join(map(str, j.get("tags") or []))])
        if not _hit([area], city_rules):
            # 城市规则存在且候选里毫无线索时，保守保留但低分；仅命中冲突才排除
            if city_rules and area.strip():
                excluded.append(j)
                continue
        if not _hit([j.get("experience") or ""], exp_rules):
            excluded.append(j)
            continue
        if not _hit([j.get("education") or ""], edu_rules):
            excluded.append(j)
            continue
        # 薪资：岗位上限高于「可接受上限」则按超档排除
        upper = _salary_annual_upper(j.get("salary") or "")
        if max_salary_cap != float("inf") and upper != float("inf") and upper > max_salary_cap:
            excluded.append(j)
            continue
        included.append(j)

    # ---- 打分（0~1）----
    # 参与打分的维度 = 配置了规则的维度（experience/education/salary/city）。
    # 分母 used_w 只计「实际参与打分维度」的权重和 → 分子分母口径一致。
    # exp/edu/salary 在硬过滤阶段已保证 included 岗位命中，故记 1；
    # city 有「无明确线索但保守保留」的岗位，需在此用 key 正常命中判断给低分。
    dims: list[tuple[str, float]] = []
    if exp_rules:
        dims.append(("experience", weights.get("experience", 0.0)))
    if edu_rules:
        dims.append(("education", weights.get("education", 0.0)))
    if salary_rules:
        dims.append(("salary", weights.get("salary", 0.0)))
    if city_rules:
        dims.append(("city", weights.get("city", 0.0)))

    if not dims:
        # 未配置任何 filters 规则：明确标记，不做伪打分（所有岗位按中性排序）
        scored = [dict(j, match=0.0, eligible=False) for j in (jobs or [])]
        return {
            "threshold": threshold,
            "note": "未配置 filters 规则，未打分（仅中性排序）",
            "total_in": len(jobs or []),
            "matched": len(scored),
            "eligible": 0,
            "jobs": scored,
            "excluded": excluded,
        }

    used_w = sum(w for _, w in dims) or 1.0
    scored: list[dict[str, Any]] = []
    for j in included:
        hit: list[str] = []
        for name, _w in dims:
            if name == "experience":
                if _hit([j.get("experience") or ""], exp_rules):
                    hit.append(name)
            elif name == "education":
                if _hit([j.get("education") or ""], edu_rules):
                    hit.append(name)
            elif name == "city":
                area = " ".join(
                    [str(j.get("area") or ""), " ".join(map(str, j.get("tags") or []))]
                )
                if _hit([area], city_rules):
                    hit.append(name)
            elif name == "salary":
                # 硬过滤已排除薪资超档岗位，这里视为命中
                hit.append(name)
        score = sum(_w for name, _w in dims if name in hit) / used_w
        sc = dict(j)
        sc["match"] = round(min(score, 1.0), 3)
        sc["eligible"] = sc["match"] >= threshold
        scored.append(sc)

    scored.sort(key=lambda x: (-x.get("match", 0), -x.get("eligible", 0)))
    return {
        "threshold": threshold,
        "total_in": len(jobs or []),
        "matched": len(scored),
        "eligible": sum(1 for s in scored if s.get("eligible")),
        "jobs": scored,
        "excluded": excluded,
    }