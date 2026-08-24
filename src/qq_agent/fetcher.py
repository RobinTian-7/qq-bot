"""抓网页 → 提正文 → 按深度跟进同域子链接。支持 HTML / PDF / 纯文本。"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from bs4 import BeautifulSoup

from .config import FetchCfg
from .db import Store

log = logging.getLogger("qq_agent.fetcher")

# 跟进子链接时跳过的东西
_SKIP_EXT = re.compile(
    r"\.(?:jpg|jpeg|png|gif|webp|svg|ico|css|js|mp3|mp4|avi|mov|zip|rar|7z|exe|dmg|apk)(?:$|\?)",
    re.I,
)
_SKIP_HREF = re.compile(r"^(?:#|javascript:|mailto:|tel:)", re.I)
# 导航/页脚这类链接的常见文案，跟进它们没意义
_NAV_WORDS = (
    "首页", "登录", "注册", "退出", "搜索", "关于我们", "联系我们", "版权", "网站地图",
    "上一页", "下一页", "更多", "返回", "打印", "English",
    "home", "login", "sign in", "sign up", "about", "contact", "search",
    "privacy", "terms", "sitemap", "download", "jobs", "community", "help",
)
# 路径里带 4 位以上数字（年份、文号、文章 id）的多半是正文页
_ID_RE = re.compile(r"\d{4,}")


@dataclass
class Page:
    url: str
    final_url: str = ""
    title: str = ""
    content: str = ""
    content_type: str = ""
    depth: int = 0
    parent_url: str | None = None
    truncated: bool = False
    error: str | None = None
    children: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.content.strip())


class Fetcher:
    def __init__(self, cfg: FetchCfg, store: Store):
        self.cfg = cfg
        self.store = store
        self._robots: dict[str, RobotFileParser | None] = {}
        self._sem = asyncio.Semaphore(cfg.concurrency)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "Fetcher":
        self._client = httpx.AsyncClient(
            timeout=self.cfg.timeout,
            follow_redirects=True,
            headers={
                "User-Agent": self.cfg.user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            limits=httpx.Limits(max_connections=self.cfg.concurrency * 2),
        )
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._client:
            await self._client.aclose()

    # ---------- 准入 ----------

    def _blocked(self, url: str) -> str | None:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return "无效链接"
        for b in self.cfg.blocked_domains:
            if host == b.lower() or host.endswith("." + b.lower()):
                return f"域名在黑名单里（{b}）"
        return None

    async def _robots_ok(self, url: str) -> bool:
        if not self.cfg.respect_robots:
            return True
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._robots:
            rp: RobotFileParser | None = None
            try:
                assert self._client
                r = await self._client.get(f"{origin}/robots.txt", timeout=8)
                if r.status_code == 200:
                    rp = RobotFileParser()
                    rp.parse(r.text.splitlines())
            except Exception:
                rp = None            # 拿不到 robots.txt 就按允许处理
            self._robots[origin] = rp
        rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(self.cfg.user_agent, url)

    # ---------- 正文提取 ----------

    def _extract_html(self, html: str, url: str) -> tuple[str, str, list[tuple[str, str]]]:
        text = (
            trafilatura.extract(
                html,
                url=url,
                include_comments=False,
                include_tables=True,
                favor_recall=True,
            )
            or ""
        )
        soup = BeautifulSoup(html, "lxml")
        title = (soup.title.get_text(strip=True) if soup.title else "") or ""

        if len(text) < 200:
            # 微信公众号 / 部分学校 CMS，trafilatura 偶尔抓空，退回按容器取
            for sel in ("#js_content", "#page-content", ".rich_media_content",
                        "article", ".article", ".content", "#content", "main"):
                node = soup.select_one(sel)
                if node:
                    cand = node.get_text("\n", strip=True)
                    if len(cand) > len(text):
                        text = cand
                    if len(text) >= 200:
                        break
        if not title:
            h = soup.find(["h1", "h2"])
            title = h.get_text(strip=True) if h else ""

        links: list[tuple[str, str]] = []
        for a in soup.find_all("a", href=True):
            links.append((a["href"], a.get_text(strip=True)))
        return title.strip(), re.sub(r"\n{3,}", "\n\n", text).strip(), links

    @staticmethod
    def _extract_pdf(data: bytes) -> tuple[str, str]:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        title = (reader.metadata.title if reader.metadata else "") or ""
        pages = [(pg.extract_text() or "") for pg in reader.pages]
        return title.strip(), re.sub(r"\n{3,}", "\n\n", "\n\n".join(pages)).strip()

    def _pick_children(self, base_url: str, links: list[tuple[str, str]]) -> list[str]:
        """从页面里挑最像「正文页」的同域链接。导航、页脚、栏目首页统统排掉。"""
        base = urlparse(base_url)
        base_host = (base.hostname or "").lower()
        scored: list[tuple[int, int, str]] = []
        seen = {urldefrag(base_url).url}

        for order, (href, label) in enumerate(links):
            if _SKIP_HREF.match(href) or _SKIP_EXT.search(href):
                continue
            full = urldefrag(urljoin(base_url, href)).url
            if not full.startswith(("http://", "https://")) or full in seen:
                continue
            host = (urlparse(full).hostname or "").lower()
            same_site = (
                host == base_host
                or host.endswith("." + base_host)
                or base_host.endswith("." + host)
            )
            # 学校/机构页面上的公众号文章链接是例外，值得跟进
            is_wechat_article = "mp.weixin.qq.com" in host
            if not (same_site or is_wechat_article):
                continue

            path = urlparse(full).path
            segs = [x for x in path.split("/") if x]
            low = (label + " " + path).lower()
            if any(w.lower() in low for w in _NAV_WORDS):
                continue

            score = 0
            if is_wechat_article:
                score += 6
            if _ID_RE.search(path):
                score += 3
            if full.lower().endswith(".pdf"):
                score += 3          # 学校通知常直接挂 PDF
            score += min(len(segs), 3)
            if len(label) >= 10:
                score += 2
            elif len(label) >= 5:
                score += 1
            if len(segs) <= 1 and not _ID_RE.search(path):
                score -= 4          # 栏目首页
            if not path or path == "/":
                continue

            if score > 0:
                seen.add(full)
                scored.append((-score, order, full))

        scored.sort()
        return [u for _, _, u in scored[: self.cfg.max_children_per_page]]

    # ---------- 单页 ----------

    async def fetch_one(self, url: str, depth: int = 0, parent: str | None = None,
                        use_cache: bool = True) -> Page:
        url = urldefrag(url).url
        if use_cache:
            cached = self.store.get_page(url)
            if cached and not cached["error"]:
                try:
                    kids = json.loads(cached["children"] or "[]")
                except (json.JSONDecodeError, KeyError):
                    kids = []
                return Page(
                    url=url, final_url=cached["final_url"], title=cached["title"],
                    content=cached["content"], content_type=cached["content_type"],
                    depth=depth, parent_url=parent, truncated=bool(cached["truncated"]),
                    children=kids if depth + 1 < self.cfg.max_depth else [],
                )

        page = Page(url=url, depth=depth, parent_url=parent)
        reason = self._blocked(url)
        if reason:
            page.error = reason
        elif not await self._robots_ok(url):
            page.error = "robots.txt 不允许抓取"
        else:
            async with self._sem:
                try:
                    assert self._client
                    r = await self._client.get(url)
                    r.raise_for_status()
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    page.final_url = str(r.url)
                    page.content_type = ctype
                    if "pdf" in ctype or str(r.url).lower().endswith(".pdf"):
                        page.title, page.content = self._extract_pdf(r.content)
                        page.content_type = "application/pdf"
                    elif "html" in ctype or "xml" in ctype or not ctype:
                        title, text, links = self._extract_html(r.text, str(r.url))
                        page.title, page.content = title, text
                        if depth + 1 < self.cfg.max_depth:
                            page.children = self._pick_children(str(r.url), links)
                    elif ctype.startswith("text/"):
                        page.content = r.text.strip()
                    else:
                        page.error = f"不支持的类型 {ctype}"
                except httpx.HTTPStatusError as e:
                    page.error = f"HTTP {e.response.status_code}"
                except httpx.TimeoutException:
                    page.error = f"超时（{self.cfg.timeout}s）"
                except Exception as e:
                    page.error = f"{type(e).__name__}: {e}"

        if page.content and len(page.content) > self.cfg.max_chars_per_page:
            page.content = page.content[: self.cfg.max_chars_per_page]
            page.truncated = True
        if not page.error and not page.content.strip():
            page.error = "没抓到正文（可能需要登录或是纯图片页）"

        self.store.save_page(page.__dict__)
        lvl = log.info if page.ok else log.warning
        lvl("[d%d] %s — %s", depth, url[:90], page.error or f"{len(page.content)} 字")
        return page

    # ---------- 批量 + 遍历 ----------

    async def crawl(self, urls: list[str], use_cache: bool = True) -> list[Page]:
        """按 max_depth 逐层展开。返回顺序：入口页在前，子页紧随其后。"""
        results: list[Page] = []
        seen: set[str] = set()
        frontier = [(u, 0, None) for u in dict.fromkeys(urls)]

        while frontier:
            batch = [(u, d, p) for u, d, p in frontier if urldefrag(u).url not in seen]
            seen.update(urldefrag(u).url for u, _, _ in batch)
            if not batch:
                break
            pages = await asyncio.gather(
                *(self.fetch_one(u, d, p, use_cache) for u, d, p in batch)
            )
            results.extend(pages)
            frontier = [
                (c, pg.depth + 1, pg.url)
                for pg in pages
                for c in pg.children
                if pg.depth + 1 < self.cfg.max_depth
            ]
        return results
