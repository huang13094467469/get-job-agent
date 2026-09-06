"""job_filter 打分口径测试（架构全景 A.2 回归锁）。

历史 bug：分母 used_w = sum(全部 weights.values())，把从未打分的 skill(0.35)/duty(0.15)
也计入 → filters 全空时任何岗位得分恒为 (0.20+0.15+0.15)=0.5，全部 eligible=False，无区分度。
修复后：只对「配置了规则的维度」打分，分子分母口径一致；filters 全空明确返回 note。
"""

from __future__ import annotations

from get_job_agent.agent.job_filter import filter_jobs

JOBS = [
    {
        "name": "AI 工程师", "company": "A公司", "salary": "30-50K",
        "experience": "3-5年", "education": "本科", "area": "北京", "tags": ["python", "算法"],
    },
    {
        "name": "前台", "company": "B公司", "salary": "4-6K",
        "experience": "1年", "education": "大专", "area": "上海", "tags": ["接待"],
    },
]

# 与 config/config.yaml matching.weights 一致的面板打分规则
RULES_CITY = {
    "filters": {"city": ["北京"], "experience": [], "education": [], "salary": []},
    "matching": {
        "threshold": 0.6,
        "weights": {"experience": 0.30, "education": 0.20, "salary": 0.30, "city": 0.20},
    },
}


def test_empty_rules_returns_note_not_fake_score() -> None:
    res = filter_jobs(JOBS)
    assert res["note"]  # 明确标记「未配置规则」，而不是给伪分数
    assert res["eligible"] == 0
    assert all(j["match"] == 0.0 for j in res["jobs"])
    assert res["matched"] == len(JOBS)


def test_city_rule_scores_consistent_denominator() -> None:
    """仅配置 city 规则 → 只有 city 维度参与打分，分母=0.20。"""
    res = filter_jobs(JOBS, rules=RULES_CITY)
    by_name = {j["name"]: j for j in res["jobs"]}
    # 命中北京 且 未被硬过滤排除 的岗位 → 满分
    assert by_name["AI 工程师"]["match"] == 1.0
    # 上海岗位在硬过滤阶段被排除（city 规则存在且 area 有内容但未命中）
    assert "前台" in [j["name"] for j in res["excluded"]]
    # matched 仅含 included 岗位
    assert "AI 工程师" in [j["name"] for j in res["jobs"]]


def test_full_rules_all_included_score_full() -> None:
    """四个维度都有规则且岗位全部命中 → included 全满分。"""
    rules = {
        "filters": {
            "city": ["北京"], "experience": ["3-5年"], "education": ["本科"], "salary": ["60K"],
        },
        "matching": {"threshold": 0.6, "weights": RULES_CITY["matching"]["weights"]},
    }
    res = filter_jobs([JOBS[0]], rules=rules)
    assert len(res["jobs"]) == 1
    assert res["jobs"][0]["match"] == 1.0
    assert res["jobs"][0]["eligible"] is True
    assert res["excluded"] == []