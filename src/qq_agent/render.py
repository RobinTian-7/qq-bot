"""把结构化日报渲染成 Markdown / HTML。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .summarizer import Digest, DigestItem

_TPL_DIR = Path(__file__).parent / "templates"

BADGE = {"高": "🔴", "中": "🟡", "低": "⚪"}
WEEKDAY = "一二三四五六日"


def _sorted_items(items: list[DigestItem]) -> list[DigestItem]:
    rank = {"高": 0, "中": 1, "低": 2}
    return sorted(items, key=lambda i: (rank.get(i.importance, 3), i.deadline or "9999-99-99"))


def _day_label(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d")
    return f"{day}（周{WEEKDAY[d.weekday()]}）"


def to_markdown(day: str, digest: Digest, meta: dict) -> str:
    L: list[str] = [f"# 班级群日报 · {_day_label(day)}", "", f"> {digest.headline}", ""]

    if digest.urgent:
        L += ["## ⏰ 马上要做", ""]
        L += [f"- {u}" for u in digest.urgent]
        L.append("")

    items = _sorted_items(digest.items)
    if not items:
        L += ["## 今天没有需要处理的信息", ""]
    else:
        L += [f"## 全部事项（{len(items)} 条）", ""]

    for n, it in enumerate(items, 1):
        head = f"### {n}. {BADGE.get(it.importance, '')} {it.title}"
        L += [head, ""]
        tags = [f"`{it.category}`", f"重要度 **{it.importance}**"]
        if it.deadline:
            tags.append(f"截止 **{it.deadline}**")
        L += ["　".join(tags), "", it.summary, ""]
        if it.key_points:
            L += [f"- {p}" for p in it.key_points] + [""]
        if it.actions:
            L += ["**要做的事**", ""] + [f"- [ ] {a}" for a in it.actions] + [""]
        if it.source_urls:
            L += ["**原文链接**", ""] + [f"- {u}" for u in it.source_urls] + [""]
        if it.source_message_ids:
            L += [f"<sub>来源消息：{', '.join('#' + str(m) for m in it.source_message_ids)}</sub>", ""]

    if digest.notes:
        L += ["---", "", "## 备注", "", digest.notes, ""]

    L += [
        "---",
        "",
        f"<sub>统计自 {meta.get('n_messages', 0)} 条群消息 · "
        f"读取网页 {meta.get('n_pages_ok', 0)} 个"
        + (f" · 抓取失败 {meta['n_pages_failed']} 个" if meta.get("n_pages_failed") else "")
        + (f" · 因过长未纳入 {meta['n_pages_dropped']} 个" if meta.get("n_pages_dropped") else "")
        + f" · 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}</sub>",
        "",
    ]
    return "\n".join(L)


def to_html(day: str, digest: Digest, meta: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(_TPL_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    tpl = env.get_template("report.html.j2")
    return tpl.render(
        day=day,
        day_label=_day_label(day),
        digest=digest,
        items=_sorted_items(digest.items),
        meta=meta,
        badge=BADGE,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


def write_reports(out_dir: Path, day: str, digest: Digest, meta: dict,
                  formats: list[str]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if "markdown" in formats:
        p = out_dir / f"{day}.md"
        p.write_text(to_markdown(day, digest, meta), "utf-8")
        written.append(p)
    if "html" in formats:
        p = out_dir / f"{day}.html"
        p.write_text(to_html(day, digest, meta), "utf-8")
        written.append(p)
    return written


def to_qq_text(day: str, digest: Digest) -> str:
    """给 QQ 私聊用的纯文本版（QQ 不支持 Markdown）。"""
    L = [f"📋 班级群日报 {_day_label(day)}", digest.headline, ""]
    if digest.urgent:
        L += ["⏰ 马上要做："] + [f"· {u}" for u in digest.urgent] + [""]
    for n, it in enumerate(_sorted_items(digest.items), 1):
        line = f"{n}. {BADGE.get(it.importance, '')}{it.title}"
        if it.deadline:
            line += f"（截止 {it.deadline}）"
        L.append(line)
        L.append(f"   {it.summary}")
        L += [f"   ▸ {a}" for a in it.actions]
        L += [f"   🔗 {u}" for u in it.source_urls[:2]]
    if digest.notes:
        L += ["", f"备注：{digest.notes}"]
    return "\n".join(L)
