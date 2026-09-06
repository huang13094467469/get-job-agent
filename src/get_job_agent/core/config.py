"""配置管理：环境变量（.env）加载连接串/凭证，YAML 加载业务规则。"""

from __future__ import annotations

import enum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProvider(str, enum.Enum):
    """推理 LLM 提供商：deepseek（官方）或 lmstudio（本地）。"""

    DEEPSEEK = "deepseek"
    LMSTUDIO = "lmstudio"


class Settings(BaseSettings):
    """连接串与凭证，一律来自环境变量 / .env，不落盘明文。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 当前推理使用的 LLM 提供商（切换 DeepSeek / 本地 LM Studio）
    llm_provider: LLMProvider = LLMProvider.DEEPSEEK

    # ---- DeepSeek 官方（OpenAI 兼容，外发，需 API Key）----
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_api_key: str = ""

    # ---- 本地 LM Studio（OpenAI 兼容，不外发，无 Key）----
    lmstudio_base_url: str = "http://192.168.1.20:1234/v1"
    lmstudio_model: str = "qwen/qwen3.5-9b"
    lmstudio_api_key: str = ""

    # 持久化 PostgreSQL
    postgres_host: str = "192.168.1.200"
    postgres_port: int = 5432
    postgres_user: str = "root"
    postgres_password: str = ""
    postgres_db: str = "boss_agent"
    # checkpoint 持久化单独建库（与业务库分离）；表由 AsyncPostgresSaver.setup() 自动创建。
    # 支撑崩溃续跑 / HITL 暂停点 / 无人值守进度跨重启精确恢复（going-to-production.md Durability）。
    checkpoint_db: str = "get_job_agent_ckpt"

    # Local Agent Server
    server_host: str = "127.0.0.1"
    server_port: int = 8791

    # ===== 运行观测 / 追踪（Tracing）=====
    # LangSmith 云端追踪：默认关闭；开启后 create_deep_agent 的每一步（模型/工具/
    # 耗时/token）自动上报，可在 LangSmith Studio 查看 trace 树并做评估。
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "get-job-agent"
    langsmith_endpoint: str = ""  # 留空用官方 smith.langchain.com；自建填 URL

    # 本地结构化 trace：无外网/不外发时，把每次运行的事件以 JSONL 落盘供离线回放与评估。
    trace_local_enabled: bool = True
    trace_file: str = "logs/agent_traces.jsonl"
    # 连续同类工具软错误达到该次数即升级为显式告警（定位「页面断开仍反复重试」这类问题）
    trace_error_streak: int = 3

    # ===== Agent 运行模式（自主程度）=====
    # confirm    = 每次发打招呼前暂停，等用户在面板确认（human-in-the-loop）
    # unattended = 无人值守：自动发送（不暂停）+ 自动续跑，逐岗位走完闭环直到无更多达标岗位/触顶
    agent_mode: str = "unattended"
    # 单轮（thread）自动打招呼的安全上限；0 = 不限（真·无人值守直到列表跑完）
    max_greetings_per_run: int = 20

    # 简历目录：用户直接把简历(PDF/DOCX)放到本目录，server 启动时自动解析去重
    attachment_dir: str = "jianli"

    # 视觉模型（兜底，本次仅记录配置不调用）—— 与对话模型同源 LM Studio
    vision_base_url: str = "http://192.168.1.20:1234/v1"
    vision_model: str = "qwen/qwen3-vl-4b"
    vision_api_key: str = ""

    @property
    def llm_base_url(self) -> str:
        """当前提供商对应的对话模型 base_url。"""
        if self.llm_provider == LLMProvider.DEEPSEEK:
            return self.deepseek_base_url
        return self.lmstudio_base_url

    @property
    def llm_model(self) -> str:
        """当前提供商对应的对话模型名。"""
        if self.llm_provider == LLMProvider.DEEPSEEK:
            return self.deepseek_model
        return self.lmstudio_model

    @property
    def llm_api_key(self) -> str:
        """当前提供商对应的 API Key（本地可空）。"""
        if self.llm_provider == LLMProvider.DEEPSEEK:
            return self.deepseek_api_key
        return self.lmstudio_api_key

    @property
    def postgres_dsn(self) -> str:
        """构建 SQLAlchemy async 连接串。"""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def checkpoint_dsn(self) -> str:
        """psycopg3（AsyncPostgresSaver）连接串：指向单独的 checkpoint 库。

        注意与 postgres_dsn 区别：psycopg3 用原生 ``postgresql://`` 驱动前缀（非 +asyncpg）。
        """
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.checkpoint_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """进程内复用的 Settings 单例。"""
    return Settings()


def load_yaml_config(path: str | Path = "config/config.yaml") -> dict[str, Any]:
    """加载业务配置（过滤规则 / prompt / 选择器 / 阈值）。

    文件不存在时返回空 dict，便于在没有配置文件的环境下启动。
    """
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


__all__ = ["Settings", "LLMProvider", "get_settings", "load_yaml_config"]