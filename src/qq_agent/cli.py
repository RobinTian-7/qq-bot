"""命令行入口。"""
from __future__ import annotations

import argparse
import asyncio
import re
import logging
import sys
from datetime import date, datetime

from rich.console import Console
from rich.logging import RichHandler

from .config import Config
from .db import Store
from .pipeline import generate

console = Console()


def _setup_log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("trafilatura").setLevel(logging.ERROR)


def _parse_day(s: str | None) -> date:
    if not s:
        return date.today()
    return datetime.strptime(s, "%Y-%m-%d").date()


# ---------- 子命令 ----------

async def cmd_watch(cfg: Config, store: Store, _a) -> int:
    from .collector import Collector

    console.print("[bold]开始监听群消息[/]（Ctrl-C 停止）")
    await Collector(cfg, store).run_forever()
    return 0


async def cmd_run(cfg: Config, store: Store, _a) -> int:
    """常驻：一边监听，一边每天定点出日报。"""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    from .collector import Collector

    hh, mm = (int(x) for x in cfg.report.daily_at.split(":"))

    async def job() -> None:
        try:
            digest, meta, files = await generate(cfg, store)
            if digest:
                console.print(f"[green]日报已生成：[/]{', '.join(str(f) for f in files)}")
        except Exception:
            logging.getLogger("qq_agent").exception("定时生成日报失败")

    sched = AsyncIOScheduler()
    sched.add_job(job, CronTrigger(hour=hh, minute=mm), id="daily", misfire_grace_time=3600)
    sched.start()
    console.print(f"[bold]监听中[/]，每天 {cfg.report.daily_at} 自动出日报（Ctrl-C 停止）")
    await Collector(cfg, store).run_forever()
    return 0


async def cmd_report(cfg: Config, store: Store, a) -> int:
    if a.provider:
        cfg.summary.provider = a.provider
    if a.group:
        want = str(a.group)
        hit = [g for g in cfg.group.group_ids
               if str(g) == want or cfg.group.name_of(g) == want]
        if not hit:
            known = "、".join(f"{g}（{cfg.group.name_of(g)}）" for g in cfg.group.group_ids)
            console.print(f"[red]没有这个群：{want}[/]  已配置的是：{known}")
            return 1
        cfg.group.group_ids = hit
        a._slug = "-" + re.sub(r"[^\w\u4e00-\u9fff-]+", "", cfg.group.name_of(hit[0]))
    digest, meta, files = await generate(
        cfg, store, day=_parse_day(a.date), use_cache=not a.no_cache,
        dry_run=a.dry_run, slug=getattr(a, "_slug", ""),
    )
    if not files:
        return 1
    for f in files:
        console.print(f"[green]✓[/] {f}")
    if digest:
        console.rule(f"[bold]今日概要[/]  [dim]({meta.get('provider')} · {meta.get('model', '')})")
        console.print(digest.headline)
        for u in digest.urgent:
            console.print(f"  ⏰ {u}")
        for it in digest.items:
            due = f"  截止 {it.deadline}" if it.deadline else ""
            console.print(f"  [{it.importance}] {it.title}{due}")
    return 0


async def cmd_backfill(cfg: Config, store: Store, a) -> int:
    from .collector import Collector

    n = await Collector(cfg, store).backfill(count=a.count)
    console.print(f"[green]补抓完成，新增 {n} 条消息[/]")
    return 0


async def cmd_fetch(cfg: Config, store: Store, a) -> int:
    from .fetcher import Fetcher

    async with Fetcher(cfg.fetch, store) as f:
        pages = await f.crawl([a.url], use_cache=False)
    for p in pages:
        console.rule(f"{p.url}")
        if p.error:
            console.print(f"[red]{p.error}[/]")
        else:
            console.print(f"[bold]{p.title}[/]  ({len(p.content)} 字"
                          f"{'，已截断' if p.truncated else ''})")
            console.print(p.content[:1500] + ("…" if len(p.content) > 1500 else ""))
    return 0


async def cmd_stats(cfg: Config, store: Store, _a) -> int:
    s = store.stats()
    console.print(f"消息 {s['messages']} 条（老师 {s['teacher_messages']} 条） · "
                  f"网页 {s['pages']} 个 · 日报 {s['digests']} 份")
    console.print(f"数据库：{cfg.resolve(cfg.storage.db_path)}")
    return 0


COMMANDS = {
    "run": cmd_run,
    "watch": cmd_watch,
    "report": cmd_report,
    "backfill": cmd_backfill,
    "fetch": cmd_fetch,
    "stats": cmd_stats,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qq-agent", description="QQ 群日报 Bot")
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="常驻：监听群消息 + 每天定点出日报（日常就用这个）")
    sub.add_parser("watch", help="只监听并存库，不出日报")

    r = sub.add_parser("report", help="立刻生成一次日报")
    r.add_argument("-d", "--date", help="日期 YYYY-MM-DD，默认今天")
    r.add_argument("--no-cache", action="store_true", help="强制重新抓取所有链接")
    r.add_argument("--dry-run", action="store_true", help="只导出送给模型的内容，不调 API")
    r.add_argument("--provider", choices=["deepseek", "codex"],
                   help="临时换一个摘要后端，不改 config.toml")
    r.add_argument("-g", "--group",
                   help="只出某一个群的日报，填群号或 [group].names 里的名字")

    b = sub.add_parser("backfill", help="从协议端补抓历史消息")
    b.add_argument("-n", "--count", type=int, default=200, help="每个群最多补抓多少条")

    f = sub.add_parser("fetch", help="试抓一个链接，看看正文提取效果")
    f.add_argument("url")

    sub.add_parser("stats", help="看看库里攒了多少数据")
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    _setup_log(a.verbose)
    cfg = Config.load(a.config)
    store = Store(cfg.resolve(cfg.storage.db_path))
    try:
        return asyncio.run(COMMANDS[a.cmd](cfg, store, a))
    except KeyboardInterrupt:
        console.print("\n[dim]已停止[/]")
        return 130
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
