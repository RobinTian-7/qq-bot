"""配置加载：config.toml + .env"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class OneBotCfg:
    ws_url: str = "ws://127.0.0.1:3001"
    access_token: str = ""
    reconnect_delay: int = 5


@dataclass
class GroupCfg:
    group_ids: list[int] = field(default_factory=list)
    teacher_qqs: list[int] = field(default_factory=list)
    include_admins: bool = True
    teacher_name_keywords: list[str] = field(default_factory=list)

    @property
    def filters_teachers(self) -> bool:
        """三个条件全空时收录所有人。"""
        return bool(self.teacher_qqs or self.include_admins or self.teacher_name_keywords)


@dataclass
class FetchCfg:
    max_depth: int = 1
    max_children_per_page: int = 5
    max_chars_per_page: int = 20000
    max_total_chars: int = 400000
    timeout: int = 25
    concurrency: int = 4
    respect_robots: bool = True
    user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
    blocked_domains: list[str] = field(default_factory=list)


@dataclass
class DeepSeekCfg:
    model: str = "deepseek-v4-pro"
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"
    max_tokens: int = 16000
    # 上下文是 1M，这个是自己设的闸，防止某天链接特别多把账单打上去
    max_input_tokens: int = 400_000
    reasoning_effort: str = "high"   # low | medium | high
    thinking: bool = True
    timeout: int = 600


@dataclass
class CodexCfg:
    command: str = "codex"
    model: str = ""                  # 留空 = 用 codex 自己的默认模型
    sandbox: str = "read-only"
    timeout: int = 900


@dataclass
class SummaryCfg:
    provider: str = "deepseek"       # deepseek | codex
    audience: str = "一位家长/学生"
    deepseek: DeepSeekCfg = field(default_factory=DeepSeekCfg)
    codex: CodexCfg = field(default_factory=CodexCfg)


@dataclass
class ReportCfg:
    out_dir: str = "reports"
    daily_at: str = "21:30"
    window_hours: int = 24
    formats: list[str] = field(default_factory=lambda: ["markdown", "html"])


@dataclass
class StorageCfg:
    db_path: str = "data/qq_agent.db"


def _summary_cfg(raw: dict) -> SummaryCfg:
    """[summary] 下面还有 [summary.deepseek] / [summary.codex] 两个子表。"""
    raw = dict(raw)
    ds = DeepSeekCfg(**(raw.pop("deepseek", {}) or {}))
    cx = CodexCfg(**(raw.pop("codex", {}) or {}))
    return SummaryCfg(deepseek=ds, codex=cx, **raw)


@dataclass
class Config:
    onebot: OneBotCfg
    group: GroupCfg
    fetch: FetchCfg
    summary: SummaryCfg
    report: ReportCfg
    storage: StorageCfg
    root: Path

    @classmethod
    def load(cls, path: str | Path = "config.toml") -> "Config":
        load_dotenv()
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise SystemExit(
                f"找不到配置文件 {p}\n先执行：cp config.example.toml config.toml，然后填上群号。"
            )
        raw = tomllib.loads(p.read_text("utf-8"))

        def sec(name: str) -> dict:
            return raw.get(name, {}) or {}

        cfg = cls(
            onebot=OneBotCfg(**sec("onebot")),
            group=GroupCfg(**sec("group")),
            fetch=FetchCfg(**sec("fetch")),
            summary=_summary_cfg(sec("summary")),
            report=ReportCfg(**sec("report")),
            storage=StorageCfg(**sec("storage")),
            root=p.parent,
        )
        if not cfg.group.group_ids:
            raise SystemExit("config.toml 里 [group].group_ids 还是空的，填上要监听的群号。")
        return cfg

    def resolve(self, rel: str) -> Path:
        q = Path(rel)
        return q if q.is_absolute() else (self.root / q)
