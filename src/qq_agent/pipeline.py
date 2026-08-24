"""日报生成流水线：取消息 → 抓链接 → 摘要 → 渲染。"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .config import Config
from .db import Store
from .fetcher import Fetcher, Page
from .render import write_reports
from .summarizer import Digest, make_summarizer

log = logging.getLogger("qq_agent.pipeline")


def day_window(cfg: Config, day: date) -> tuple[int, int]:
    """返回该日报覆盖的 [start_ts, end_ts)。以 daily_at 为界往前推 window_hours。"""
    hh, mm = (int(x) for x in cfg.report.daily_at.split(":"))
    end = datetime.combine(day, time(hh, mm))
    start = end - timedelta(hours=cfg.report.window_hours)
    return int(start.timestamp()), int(end.timestamp())


async def generate(
    cfg: Config,
    store: Store,
    day: date | None = None,
    use_cache: bool = True,
    dry_run: bool = False,
) -> tuple[Digest | None, dict, list[Path]]:
    day = day or date.today()
    day_str = day.isoformat()
    start_ts, end_ts = day_window(cfg, day)
    log.info(
        "统计区间：%s → %s",
        datetime.fromtimestamp(start_ts).strftime("%m-%d %H:%M"),
        datetime.fromtimestamp(end_ts).strftime("%m-%d %H:%M"),
    )

    msgs = store.messages_between(
        cfg.group.group_ids, start_ts, end_ts, teachers_only=cfg.group.filters_teachers
    )
    log.info("命中 %d 条消息", len(msgs))
    if not msgs:
        log.warning("这段时间没有消息，不生成日报。")
        return None, {"n_messages": 0}, []

    urls: list[str] = []
    for m in msgs:
        urls.extend(m["urls"])
    urls = list(dict.fromkeys(urls))
    log.info("发现 %d 个链接，开始抓取（深度上限 %d）", len(urls), cfg.fetch.max_depth)

    pages: list[Page] = []
    if urls:
        async with Fetcher(cfg.fetch, store) as f:
            pages = await f.crawl(urls, use_cache=use_cache)
    log.info("成功读取 %d / %d 个页面", sum(1 for p in pages if p.ok), len(pages))
    log.info("摘要后端：%s", cfg.summary.provider)

    summ = make_summarizer(cfg)
    if dry_run:
        content, meta = summ.build_prompt(day_str, msgs, pages)
        out = cfg.resolve(cfg.report.out_dir) / f"{day_str}.prompt.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, "utf-8")
        log.info("dry-run：没有调用模型，输入内容已写到 %s（%d 字）", out, len(content))
        return None, meta, [out]

    digest, meta = summ.summarize(day_str, msgs, pages)
    store.save_digest(day_str, digest.model_dump(), meta.get("usage", {}))
    written = write_reports(
        cfg.resolve(cfg.report.out_dir), day_str, digest, meta, cfg.report.formats
    )
    return digest, meta, written
