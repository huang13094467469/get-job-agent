"""Agent 简历携带工具（M6）：把简历以**摘要**形式接入 Agent 上下文，并在筛选岗位后做**按模块**比对。

设计（对齐用户要求）：
- 不把完整简历塞进上下文（token 过多）。
- `get_resume_summary`：取最新附件简历，压缩成「主要内容摘要」给 Agent 参考。
- `compare_job_with_resume`：Agent 把 snapshot 返回的岗位对象 jobs[i] **原样**传入，
  服务端自动兼容键名归一（name/job_name、area/city、experience/years、tags/skills），
  再与简历按模块（岗位技能 / 年限 / 学历 / 薪资 / 城市）逐项比对，产出逐条 verdict 与综合分；
  字段缺口过大时提示先去详情页补全 JD，而不是凭残缺信息直接投递。
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command

from ...core.config import get_settings, load_yaml_config
from ...infra.resume_store import ResumeStore
from .. import job_ledger

# 简历比对五模块权重（技能/年限/学历/薪资/城市）。
# 以 config.yaml 的 matching.compare_weights 为唯一事实源；配置缺失时回退默认值。
_DEFAULT_WEIGHTS = {
    "skill": 0.35,
    "experience": 0.20,
    "education": 0.15,
    "salary": 0.15,
    "city": 0.15,
}


def _load_compare_weights() -> dict[str, float]:
    """读取 config.yaml matching.compare_weights.weights 的简历比对权重。"""
    cfg = load_yaml_config("config/config.yaml") or {}
    cw = (cfg.get("matching") or {}).get("compare_weights") or {}
    weights = cw.get("weights") or {}
    return {k: float(weights.get(k, v)) for k, v in _DEFAULT_WEIGHTS.items()}


_WEIGHTS = _load_compare_weights()


def _load_compare_threshold() -> float:
    """读取 config.yaml matching.compare_weights.threshold；缺失回退 0.6。"""
    cfg = load_yaml_config("config/config.yaml") or {}
    cw = (cfg.get("matching") or {}).get("compare_weights") or {}
    return float(cw.get("threshold", 0.6))


# 达标判定阈值（技能/年限/学历/薪资/城市模块的重叠确认）。
# unknown 模块权重占比 ≥ 该比例的岗位视为信息不足，不能凭残缺信息投递。
_ELIGIBLE_THRESHOLD = _load_compare_threshold()
_THIN_DATA_FRACTION = 0.4


def _text(v: Any) -> str:
    return str(v or "").strip()


def _norm(s: str) -> str:
    return re.sub(r"[，,、/\\|；;\s]+", "", _text(s)).lower()


async def _load_latest_profile() -> dict[str, Any] | None:
    """取最新附件简历的画像 dict（profile_json 内容）。"""
    store = ResumeStore(get_settings())
    try:
        row = await store.latest_attachment("default")
    finally:
        await store.dispose()
    if not row:
        return None
    pj = row.get("profile_json")
    return pj if isinstance(pj, dict) else None


def _condense_profile(profile: dict[str, Any]) -> str:
    """压缩完整画像为紧凑摘要（主要字段，控制 token）。"""
    intent = profile.get("intent") or {}
    skills = profile.get("skills") or []
    exps = profile.get("experiences") or []
    lines = [
        f"姓名: {_text(profile.get('name'))}",
        f"意向岗位: {_text(intent.get('position'))}",
    ]
    smin, smax = intent.get("salary_min"), intent.get("salary_max")
    lines.append(f"薪资期望: {'-'.join(x for x in [_text(smin), _text(smax)] if x) or '不限'}")
    lines.append(f"工作年限: {_text(profile.get('years'))}")
    lines.append(f"学历: {_text(profile.get('education'))}")
    if skills:
        lines.append("核心技能: " + "、".join(str(s) for s in skills[:8]))
    for e in exps[:2]:
        lines.append(
            f"经历: {_text(e.get('title'))} @ {_text(e.get('company'))} "
            f"({_text(e.get('start'))}~{_text(e.get('end'))}) {_text(e.get('summary'))[:80]}"
        )
    return "\n".join(lines)


def _exp_years(profile_years: Any) -> int:
    m = re.findall(r"\d+", _text(profile_years))
    return int(m[0]) if m else 0


def _edu_value(edu: str) -> int:
    """学历等级，越大越高：不限0 大专1 本科2 硕士3 博士4。"""
    e = _norm(edu)
    if "博士" in e:
        return 4
    if "硕士" in e:
        return 3
    if "本科" in e:
        return 2
    if "大专" in e:
        return 1
    return 0


def _salary_monthly_upper_k(salary: Any) -> float | None:
    """把 Boss 薪资文本统一折算成「月薪上限（K）」；无法解析返回 None。

    必须同单位才能比：简历 intent.salary_min/max 是**期望月薪（数字，单位 K）**，
    而岗位文本可能是「20-30K」/「15-25万」/「8000-12000」/「300-500元/天」。
    """
    text = re.sub(r"[·・\-—\s]*\d+\s*薪", "", _text(salary))  # 去掉「·14薪」这类月数倍率，否则会被当成薪资数字
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    if not nums:
        return None
    hi = max(nums)
    if "万" in text:            # 「15-25万」按年薪折算月薪
        return hi * 10000.0 / 12.0 / 1000.0
    if "天" in text or "日" in text:  # 「300-500元/天」按 22 个工作日
        return hi * 22 / 1000.0
    if "k" in text.lower() or "千" in text:
        return hi
    if hi >= 1000:              # 「8000-12000」元/月
        return hi / 1000.0
    return hi                   # Boss 裸数字（如「20-30」）默认就是 K


# 岗位字段键名兼容：把 snapshot 的 jobs[i] 对象 / JD 文本都归一到规范键
_JOB_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "position": ("position", "job_name", "jobName", "name", "title"),
    "company": ("company", "company_name", "companyName", "boss_name"),
    "city": ("city", "area", "job_area", "location"),
    "salary": ("salary", "wage", "pay"),
    "years": ("years", "experience", "exp", "work_years"),
    "education": ("education", "edu", "degree"),
    "skills": ("skills", "tags", "tag"),
    "duty": ("duty", "responsibility", "description", "desc", "jd"),
}


def _pick_key(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def _normalize_job(d: dict[str, Any]) -> dict[str, Any]:
    """把任意键名的岗位 dict 归一为规范字段（position/city/years/education/skills/salary/duty）。"""
    out: dict[str, Any] = {}
    for canon, aliases in _JOB_KEY_ALIASES.items():
        out[canon] = _pick_key(d, aliases)
    sk = out.get("skills")
    if isinstance(sk, str):
        out["skills"] = [s for s in re.split(r"[、,，/|;；\s]+", sk) if s]
    elif isinstance(sk, list):
        out["skills"] = [str(s) for s in sk if str(s).strip()]
    else:
        out["skills"] = []
    return out


def _job_from_text(text: str) -> dict[str, Any]:
    """无结构化对象时，从 JD 文本尽力抽取关键字段（首行岗位名/薪资/年限/学历），供低置信兜底。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    position = lines[0][:40] if lines else ""
    m_sal = re.search(
        r"\d+(?:\.\d+)?\s*[-~至]\s*\d+(?:\.\d+)?\s*[kK千]|\d+(?:\.\d+)?\s*[kK千]", text
    )
    m_yr = re.search(r"\d+\s*[-~]\s*\d+\s*年|\d+\s*年以上|\d+\s*年经验", text)
    edu = ""
    for e in ("博士", "硕士", "本科", "大专"):
        if e in text:
            edu = e
            break
    return {
        "position": position,
        "salary": m_sal.group(0) if m_sal else "",
        "years": m_yr.group(0) if m_yr else "",
        "education": edu,
        "skills": [],
        "duty": text[:500],
    }


def _parse_job_input(raw: str) -> dict[str, Any] | None:
    """把 job 入参解析成规范字段 dict：优先 JSON 对象（含 snapshot 的 jobs[i]），否则按 JD 文本抽取。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001  不是 JSON → 当作 JD 文本
        return _normalize_job(_job_from_text(raw))
    if isinstance(obj, dict):
        return _normalize_job(obj)
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return _normalize_job(obj[0])  # 误传成数组，取第一条
    return _normalize_job(_job_from_text(raw))


@tool
async def get_resume_summary() -> str:
    """读取简历主要内容的紧凑摘要（姓名/意向岗位/薪资期望/年限/学历/核心技能/近两段经历）。

    开始搜岗或判断匹配前调用一次，把摘要作为自己的背景。避免一次性载入完整简历以省 token。
    """
    profile = await _load_latest_profile()
    if not profile:
        return "未找到已解析的附件简历：请确认 jianli 目录下有简历且已扫描入库。"
    return _condense_profile(profile)


@tool
async def compare_job_with_resume(
    job: str, runtime: ToolRuntime, expected_city: str | None = None
) -> Command:
    """把采集到的一个岗位与简历做「按模块」详细比对并给出匹配分。

    job：直接把 browser_snapshot 岗位列表里那条岗位对象（jobs[i]）原样贴进来即可，
    键名自动兼容（position/name、city/area、years/experience、skills/tags、salary、education），
    无需自己重新拼装字段；没有结构化对象时也可整段贴入 JD 文本（服务端会抽取薪资/年限/学历）。
    缺失的字段会标注 unknown。expected_city 为候选人期望城市（可选，提供才会评城市模块）。
    返回逐模块 verdict(match/partial/mismatch/unknown) + 综合 match 分 + 结论。

    判定达标时，把本岗位写进 thread state 的 pending_job（经 Command(update)，随 checkpoint 持久），
    供 send_greeting 成功后升级为账本「已打招呼」——替代原进程级 note_pending_job 全局 dict。
    """
    def _msg(text: str, **state_update: Any) -> Command:
        """把比对结论包成 ToolMessage，并附带 state 更新（pending_job），统一经 Command 返回。"""
        update: dict[str, Any] = dict(state_update)
        update["messages"] = [ToolMessage(
            content=text, tool_call_id=runtime.tool_call_id, name="compare_job_with_resume",
        )]
        return Command(update=update)

    job_dict = _parse_job_input(job)
    if job_dict is None:
        return _msg("未识别到岗位信息：请把 snapshot 的 job 对象或 JD 文本传给 job 参数。")
    job = job_dict  # 归一后统一按规范键读取
    company = _text(job.get("company"))
    position = _text(job.get("position"))
    # 过目清单短路：仅当该「公司|岗位」已为**终态**才跳过（达标待沟通/信息不足不算）
    seen = job_ledger.get_entry(job_ledger.USER_KEY, company, position)
    if job_ledger.is_terminal(seen):
        why = f"（{seen['reason']}）" if seen.get("reason") else ""
        return _msg(f"【已过目】{company} | {position} → {seen['result']}{why}"
                    "。已处理过，直接跳过，勿重复。")
    profile = await _load_latest_profile()
    if not profile:
        return _msg("未找到已解析简历，无法比对。")
    resume = profile
    intent = resume.get("intent") or {}

    modules: list[dict[str, Any]] = []

    # ---- 岗位 / 技能 ----
    job_skills = [str(s) for s in (job.get("skills") or [])]
    resume_skills = [str(s) for s in (resume.get("skills") or [])]
    hay = _norm(" ".join(resume_skills + [_text(resume.get("name"))] + [_text(intent.get("position"))]))
    pos_parts = [_norm(job.get("position") or "")] + [_norm(s) for s in job_skills]
    hit = [p for p in pos_parts if p and p in hay]
    if not hit and not job_skills:
        # 无技能标签的兜底路径（整段 JD 文本）：反向拿简历技能去碰岗位正文
        duty_hay = _norm(" ".join([_text(job.get("duty")), _text(job.get("position"))]))
        if duty_hay:
            hit = [s for s in resume_skills if _norm(s) and _norm(s) in duty_hay]
    if not job.get("position") and not job_skills:
        modules.append({"module": "岗位/技能", "verdict": "unknown",
                        "detail": "未提供岗位/技能信息", "weight": _WEIGHTS["skill"]})
    elif hit:
        modules.append({"module": "岗位/技能", "verdict": "match",
                        "detail": f"命中技能关键字: {'、'.join(hit[:6])}", "weight": _WEIGHTS["skill"]})
    else:
        modules.append({"module": "岗位/技能", "verdict": "partial",
                        "detail": "未见明显技能重叠，需结合职责进一步判断", "weight": _WEIGHTS["skill"]})

    # ---- 年限 ----
    req_years = _exp_years(job.get("years") or "")
    my_years = _exp_years(resume.get("years"))
    if not _text(job.get("years")):
        modules.append({"module": "年限", "verdict": "unknown",
                        "detail": f"简历{my_years}年，岗位未写要求", "weight": _WEIGHTS["experience"]})
    elif my_years >= req_years:
        modules.append({"module": "年限", "verdict": "match",
                        "detail": f"简历{my_years}年 ≥ 要求{req_years}年", "weight": _WEIGHTS["experience"]})
    else:
        modules.append({"module": "年限", "verdict": "mismatch",
                        "detail": f"简历{my_years}年 < 要求{req_years}年", "weight": _WEIGHTS["experience"]})

    # ---- 学历 ----
    req_edu = _text(job.get("education"))
    if not req_edu:
        modules.append({"module": "学历", "verdict": "unknown",
                        "detail": f"简历学历 {_text(resume.get('education'))}，岗位未写要求", "weight": _WEIGHTS["education"]})
    elif _edu_value(resume.get("education")) >= _edu_value(req_edu):
        modules.append({"module": "学历", "verdict": "match",
                        "detail": f"简历 {_text(resume.get('education'))} 满足 {req_edu}", "weight": _WEIGHTS["education"]})
    else:
        modules.append({"module": "学历", "verdict": "mismatch",
                        "detail": f"简历 {_text(resume.get('education'))} 不满足 {req_edu}", "weight": _WEIGHTS["education"]})

    # ---- 薪资（统一到「月薪 K」后比对）----
    job_upper_k = _salary_monthly_upper_k(job.get("salary"))
    exp_raw = _text(intent.get("salary_max")) or _text(intent.get("salary_min"))
    m_exp = re.search(r"\d+(?:\.\d+)?", exp_raw)
    if job_upper_k is None or not m_exp:
        modules.append({"module": "薪资", "verdict": "unknown",
                        "detail": f"岗位薪资 {_text(job.get('salary')) or '未写'} / 期望月薪 {exp_raw or '未填'}，信息不全",
                        "weight": _WEIGHTS["salary"]})
    elif job_upper_k >= float(m_exp.group(0)):
        modules.append({"module": "薪资", "verdict": "match",
                        "detail": f"岗位月薪上限≈{job_upper_k:.0f}K ≥ 期望{float(m_exp.group(0)):.0f}K",
                        "weight": _WEIGHTS["salary"]})
    else:
        modules.append({"module": "薪资", "verdict": "mismatch",
                        "detail": f"岗位月薪上限≈{job_upper_k:.0f}K < 期望{float(m_exp.group(0)):.0f}K",
                        "weight": _WEIGHTS["salary"]})

    # ---- 城市（仅当给出期望城市）----
    if expected_city:
        if _norm(job.get("city")) and _norm(expected_city) in _norm(job.get("city")):
            modules.append({"module": "城市", "verdict": "match",
                            "detail": f"岗位城市 {_text(job.get('city'))} 含期望 {expected_city}", "weight": _WEIGHTS["city"]})
        else:
            modules.append({"module": "城市", "verdict": "mismatch",
                            "detail": f"岗位城市 {_text(job.get('city')) or '未知'}，期望 {expected_city}", "weight": _WEIGHTS["city"]})

    # ---- 综合分 ----
    weight_map = {"match": 1.0, "partial": 0.5, "unknown": 0.6, "mismatch": 0.0}
    used_w = sum(m["weight"] for m in modules) or 1.0
    unknown_w = sum(m["weight"] for m in modules if m["verdict"] == "unknown")
    score = sum(weight_map[m["verdict"]] * m["weight"] for m in modules) / used_w
    # unknown 按 0.6 计分，若占比过大则“全 unknown”也能蹭到阈值 → 必须拦住
    thin_data = (unknown_w / used_w) >= _THIN_DATA_FRACTION
    eligible = score >= _ELIGIBLE_THRESHOLD and not thin_data

    lines = ["逐模块比对:"]
    for m in modules:
        lines.append(f"- {m['module']}: {m['verdict']}（{m['detail']}）")
    sc = round(min(score, 1.0), 3)
    lines.append(f"综合匹配分: {sc}")
    state_update: dict[str, Any] = {}
    if thin_data:
        lines.append("结论: 岗位字段缺口过大（unknown 权重占比≥40%），"
                     "先点进详情页补全 JD 后重新比对，暂不投递")
        # 信息不足：记非终态（不阻断重评，仅留痕）
        job_ledger.record(job_ledger.USER_KEY, company, position,
                          "信息不足", reason="需补全JD后重评")
    elif eligible:
        lines.append("结论: 符合，可进入沟通环节")
        # 达标：把本岗位写进 thread state 的 pending_job（供 send_greeting 成功后升级为账本「已打招呼」）
        # + 记非终态留痕。迁入 custom state（随 checkpoint 持久）替代进程级 note_pending_job 全局 dict。
        state_update["pending_job"] = {"company": company, "position": position, "link": None}
        job_ledger.record(job_ledger.USER_KEY, company, position,
                          "达标待沟通", reason=f"匹配分{sc}")
    else:
        lines.append("结论: 匹配偏弱，谨慎沟通")
        # 明确不达标（终态）：登记「不匹配跳过」，避免下一轮重复评估
        job_ledger.record(job_ledger.USER_KEY, company, position,
                          "不匹配跳过", reason=f"匹配分{sc}")
    return _msg("\n".join(lines), **state_update)


__all__ = ["get_resume_summary", "compare_job_with_resume"]