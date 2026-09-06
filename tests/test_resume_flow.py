"""简历画像 JSONB 写入口径测试（架构全景 A.1 回归锁）。

历史 bug：resume_service 把 extract_resume 返回的 str 直接传给 insert，
ResumeStore._as_json 对 str 再 dumps 一次 → JSONB 存成字符串标量 "{\"name\":...}"，
读取链 _decode_profile 只解一层拿到 str → compare/get_summary 全拿不到 dict。
"""

from __future__ import annotations

import json

from get_job_agent.infra.resume_store import _as_json


def test_as_json_dict_roundtrip() -> None:
    d = {"name": "huang", "skills": ["python"]}
    encoded = _as_json(d)
    assert json.loads(encoded) == d


def test_as_json_str_is_decoded_once() -> None:
    """传入已是 JSON 字符串时应解一层再编码，不得二次编码成字符串标量。"""
    d = {"name": "huang", "skills": ["python"]}
    encoded = _as_json(json.dumps(d, ensure_ascii=False))
    assert json.loads(encoded) == d  # 读回应是 dict，而非 {"...": 字符串}


def test_as_json_non_json_str_passthrough() -> None:
    assert _as_json("not json") == '"not json"'


def test_as_json_none() -> None:
    assert _as_json(None) is None


def test_resume_carry_weights_match_config() -> None:
    """A.3 回归锁：resume_carry 权重/阈值以 config.yaml compare_weights 为单一事实源。

    若未来有人在 resume_carry 里硬编码权重或改 config 不同步，本测试即失败。
    """
    from get_job_agent.agent.tools.resume_carry import _ELIGIBLE_THRESHOLD, _WEIGHTS
    from get_job_agent.core.config import load_yaml_config

    cfg = (load_yaml_config("config/config.yaml") or {}).get("matching", {}).get(
        "compare_weights"
    )
    assert cfg is not None, "config.yaml 缺少 matching.compare_weights"
    expected_w = {k: float(v) for k, v in (cfg.get("weights") or {}).items()}
    assert _WEIGHTS == expected_w
    assert _ELIGIBLE_THRESHOLD == float(cfg.get("threshold", 0.6))