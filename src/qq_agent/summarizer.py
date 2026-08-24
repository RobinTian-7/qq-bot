"""日报的数据结构、提示词与后端选择。

真正调模型的代码在 backends/ 下：
  - backends/deepseek.py  走 DeepSeek API（OpenAI 兼容接口）
  - backends/codex.py     调本机的 codex CLI
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import Config
from .fetcher import Page

log = logging.getLogger("qq_agent.summarizer")

Category = Literal["作业", "考试", "缴费", "活动", "材料提交", "课程安排", "通知", "其他"]
Importance = Literal["高", "中", "低"]


# ---------------------------------------------------------------- 数据结构

class DigestItem(BaseModel):
    title: str = Field(description="一句话标题，20 字以内，点明是什么事")
    category: Category
    importance: Importance
    summary: str = Field(description="2-4 句话说清来龙去脉")
    key_points: list[str] = Field(description="要点列表，没有就空数组")
    deadline: str = Field(description="YYYY-MM-DD 格式的截止日期；没有截止时间填空字符串")
    actions: list[str] = Field(description="用户需要做的事，祈使句；不需要做事就空数组")
    source_message_ids: list[int] = Field(description="依据的消息编号")
    source_urls: list[str] = Field(description="相关原始链接，逐字复制输入里的 URL")
    group: str = Field(description="这条事项来自哪个群，逐字复制输入里的群名")


class Digest(BaseModel):
    headline: str = Field(description="一句话概括今天最需要知道的事，30 字以内")
    urgent: list[str] = Field(description="今天或明天就要动手的事，最多 3 条；没有就空数组")
    items: list[DigestItem]
    notes: str = Field(description="整理过程中的提醒，例如某链接没抓到、某条消息含义不明；没有填空字符串")


def strict_schema(model: type[BaseModel] = Digest) -> dict[str, Any]:
    """转成严格 JSON Schema：每层对象都要 additionalProperties=false 且字段全必填。

    Codex 的 --output-schema 要求这个形状，不然会被 400 掉。
    """
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: walk(v) for k, v in node.items()}
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"].keys())
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(model.model_json_schema())


EXAMPLE = {
    "headline": "明天体检要空腹，周五前交社会实践表",
    "urgent": ["今晚把登记表打印出来给家长签字"],
    "items": [{
        "title": "8月25日全校体检，需空腹",
        "category": "通知",
        "importance": "高",
        "summary": "王老师转发校医院通知，8 月 25 日上午体检，空腹到校，8:00 体育馆集合。补检安排原文未说明。",
        "key_points": ["8:00–11:30 体育馆", "带学生证和口罩"],
        "deadline": "2026-08-25",
        "actions": ["8 月 25 日早上不要吃早饭"],
        "source_message_ids": [9001, 9002],
        "source_urls": ["https://example.edu.cn/notice/2026/0824.html"],
        "group": "三年二班",
    }],
    "notes": "有 1 个链接指向百度网盘，没能抓取，建议手动点开。",
}


# ---------------------------------------------------------------- 提示词

SYSTEM = """你是一个班级群信息整理助手。用户是{audience}。

你会收到某一天班级群里老师发布的全部消息，以及这些消息中链接所指向的网页正文（已抓取）。
你的任务：把这些原始信息整理成一份"扫一眼就知道今天要干什么"的日报。

硬性要求：
1. 只写消息和网页里真实存在的内容。任何日期、金额、地点、人名都必须能在原文中找到。
   原文没说的一律不要补全、不要推测。信息不全就在 summary 里直接写"原文未说明"。
2. 把同一件事的多条消息合并成一个条目（例如老师先发通知、后发补充说明、再发链接）。
3. deadline 一律换算成具体日期（YYYY-MM-DD）。原文说"明天""本周五"时，按输入里给出的"今天日期"推算，
   并在 summary 里注明推算依据。推算不出来就留空字符串。
   如果原文里的星期几和日期对不上，以原文写明的具体日期为准，并在 notes 里说明这个矛盾。
4. actions 写成祈使句，是用户本人要做的动作，例如"周四前把回执单签字拍照发给王老师"。
   纯知会类通知（不需要用户做任何事）actions 留空数组。
5. importance 判定：有明确截止时间、要交钱、要交材料、涉及考试 = 高；
   需要留意但不用马上动 = 中；纯通知、闲聊、鼓励的话 = 低。
6. source_message_ids 填该条目依据的消息编号（就是输入里 #号 后面的数字）。
   source_urls 填该条目相关的原始链接，必须逐字复制输入中出现过的 URL，不要自己拼。
7. 网页正文若标注了"已截断"，说明后面还有内容没读到——如果这个条目的关键信息可能在截断部分，
   在 summary 末尾加一句"（原文较长，建议点开链接确认完整内容）"。
8. 用简体中文。summary 控制在 2–4 句，key_points 每条一行、别超过 40 字。

9. 每条消息开头的【】里是群名。group 字段填该条目所属的群名，逐字照抄。
   不同群的事情不要合并成一个条目，哪怕内容看起来相似——不同班级的通知细节往往不一样。

排序：items 按 importance 高→中→低，同级按 deadline 由近到远。"""

JSON_RULES = """

输出格式：只输出一个 json 对象，不要有任何解释文字，不要用 markdown 代码块包裹。
这个 json 必须严格符合下面的 JSON Schema：

{schema}

一个合法输出的例子（只是示范结构，内容按实际输入来写）：

{example}"""


def _fmt_messages(msgs: list[dict[str, Any]], names: dict[int, str] | None = None) -> str:
    names = names or {}
    out: list[str] = []
    for m in msgs:
        t = datetime.fromtimestamp(m["ts"]).strftime("%H:%M")
        role = {"owner": "群主", "admin": "管理员"}.get(m["sender_role"], "")
        who = f"{m['sender_name']}（{role}）" if role else m["sender_name"]
        gname = names.get(m["group_id"], "")
        head = f"#{m['message_id']} | {t} | 【{gname}】{who}" if gname else f"#{m['message_id']} | {t} | {who}"
        block = [head, m["text"] or "（无文字内容）"]
        if m["urls"]:
            block.append("链接：" + "  ".join(m["urls"]))
        out.append("\n".join(block))
    return "\n\n".join(out)


def _fmt_pages(pages: list[Page]) -> tuple[str, list[str]]:
    blocks: list[str] = []
    failures: list[str] = []
    for p in pages:
        if not p.ok:
            failures.append(f"{p.url} —— {p.error}")
            continue
        head = f"URL: {p.url}"
        if p.final_url and p.final_url != p.url:
            head += f"\n实际地址: {p.final_url}"
        if p.parent_url:
            head += f"\n（从 {p.parent_url} 跟进而来）"
        head += f"\n标题: {p.title or '(无)'}"
        if p.truncated:
            head += "\n注意: 正文已截断，后面还有内容没读到"
        blocks.append(f"----- 网页正文 -----\n{head}\n\n{p.content}")
    return "\n\n".join(blocks), failures


def _apply_budget(pages: list[Page], max_total: int) -> tuple[list[Page], list[str]]:
    """总量超限时，从最长的页面开始丢，并明确记下丢了什么——绝不静默截断。"""
    ok = [p for p in pages if p.ok]
    dropped: list[str] = []
    total = sum(len(p.content) for p in ok)
    if total <= max_total:
        return pages, dropped
    keep = {id(p) for p in ok}
    for p in sorted(ok, key=lambda p: len(p.content), reverse=True):
        if total <= max_total:
            break
        keep.discard(id(p))
        total -= len(p.content)
        dropped.append(f"{p.title or p.url}（{len(p.content)} 字，{p.url}）")
    return [p for p in pages if (not p.ok) or id(p) in keep], dropped


def estimate_tokens(text: str) -> int:
    """粗略估算。DeepSeek 没有 count_tokens 接口，按官方给的经验值算：
    1 个中文字 ≈ 0.6 token，1 个英文字符 ≈ 0.3 token。只用来提示量级，不精确。
    """
    cjk = sum(
        1 for ch in text
        if "一" <= ch <= "鿿" or "　" <= ch <= "〿" or "＀" <= ch <= "￯"
    )
    return int(cjk * 0.6 + (len(text) - cjk) * 0.3)


# ---------------------------------------------------------------- 后端基类

class BaseSummarizer:
    name = "base"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._names = {gid: cfg.group.name_of(gid) for gid in cfg.group.group_ids}

    def system_prompt(self, with_json_rules: bool) -> str:
        s = SYSTEM.format(audience=self.cfg.summary.audience)
        if with_json_rules:
            s += JSON_RULES.format(
                schema=json.dumps(strict_schema(), ensure_ascii=False, indent=1),
                example=json.dumps(EXAMPLE, ensure_ascii=False, indent=1),
            )
        return s

    def build_prompt(self, day: str, msgs: list[dict], pages: list[Page]) -> tuple[str, dict]:
        pages, dropped = _apply_budget(pages, self.cfg.fetch.max_total_chars)
        page_text, failures = _fmt_pages(pages)

        wd = "一二三四五六日"[datetime.strptime(day, "%Y-%m-%d").weekday()]
        parts = [
            f"今天日期：{day}（星期{wd}）",
            f"\n\n========== 群消息（共 {len(msgs)} 条）==========\n\n",
            _fmt_messages(msgs, self._names) or "（今天没有消息）",
        ]
        if page_text:
            parts.append(f"\n\n========== 链接网页正文 ==========\n\n{page_text}")
        if failures:
            parts.append(
                "\n\n========== 以下链接没能抓取（请在 notes 里提醒用户手动点开）==========\n"
                + "\n".join(failures)
            )
        if dropped:
            parts.append(
                "\n\n========== 以下网页因内容过长未纳入本次分析（请在 notes 里如实告知）==========\n"
                + "\n".join(dropped)
            )
        meta = {
            "provider": self.name,
            "multi_group": self.cfg.group.multi,
            "n_messages": len(msgs),
            "n_pages_ok": sum(1 for p in pages if p.ok),
            "n_pages_failed": len(failures),
            "n_pages_dropped": len(dropped),
            "dropped": dropped,
            "failures": failures,
        }
        return "".join(parts), meta

    def summarize(self, day: str, msgs: list[dict], pages: list[Page]) -> tuple[Digest, dict]:
        raise NotImplementedError


def parse_digest(text: str) -> Digest:
    """容忍模型偶尔套上 markdown 代码块。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    t = t.strip()
    if not t:
        raise ValueError("模型返回了空内容")
    return Digest.model_validate_json(t)


def make_summarizer(cfg: Config) -> BaseSummarizer:
    provider = cfg.summary.provider.lower()
    if provider == "deepseek":
        from .backends.deepseek import DeepSeekSummarizer
        return DeepSeekSummarizer(cfg)
    if provider == "codex":
        from .backends.codex import CodexSummarizer
        return CodexSummarizer(cfg)
    raise SystemExit(
        f"[summary].provider 不认识：{cfg.summary.provider}（可选 deepseek / codex）"
    )
