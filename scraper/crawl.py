"""爬取主流程：列表頁翻頁 → 詳細頁抽欄位 → 量測圖片尺寸 → 寫入 SQLite。"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterator

from bs4 import BeautifulSoup

from .config import SiteConfig
from .db import Database, ImageRecord
from .extract import extract_item, extract_links, parse_html
from .fetch import Fetcher

log = logging.getLogger(__name__)


class _Budget(Exception):
    """達到筆數上限或時間上限時，用來乾淨地中止整趟爬取（進度已存好）。"""


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        joined = " ".join(str(v) for v in value if v)
        return joined or None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class Crawler:
    def __init__(
        self,
        cfg: SiteConfig,
        db: Database,
        fetcher: Fetcher | None = None,
        limit: int = 0,
        force: bool = False,
        dry_run: bool = False,
        max_runtime: float = 0.0,
        restart: bool = False,
    ):
        self.cfg = cfg
        self.db = db
        self.fetcher = fetcher or Fetcher(cfg.politeness)
        self.limit = limit or cfg.max_items
        self.force = force
        self.dry_run = dry_run
        self.restart = restart
        self.deadline = time.monotonic() + max_runtime if max_runtime else None
        self.saved = 0
        self.skipped = 0
        self.errors = 0
        self.failed: list[tuple[str, str]] = []  # 本次失敗的 (url, 原因)
        self.empty: list[str] = []  # 本次沒有圖片的頁面

    # ---------- 對外 ----------

    def run(self) -> None:
        try:
            for start in self.cfg.start_urls:
                self._crawl_listing(start)
        except _Budget as exc:
            log.info("%s，進度已保存，下次執行會從中斷處繼續", exc)
        except KeyboardInterrupt:
            log.warning("使用者中斷，保存已抓到的資料")
        finally:
            if not self.dry_run:
                self.db.commit()
        self._report()

    def crawl_urls(self, urls: list[str]) -> None:
        """直接處理指定的詳細頁清單（重試失敗頁面用）。"""
        try:
            for url in urls:
                self._crawl_detail(url)
        except _Budget as exc:
            log.info("%s", exc)
        except KeyboardInterrupt:
            log.warning("使用者中斷")
        finally:
            if not self.dry_run:
                self.db.commit()
        self._report()

    def _report(self) -> None:
        log.info(
            "完成：新增/更新 %d 筆｜跳過 %d 頁（已抓過）｜無圖片 %d 頁｜失敗 %d 頁",
            self.saved, self.skipped, len(self.empty), self.errors,
        )
        if self.empty:
            log.info("本次沒有圖片的頁面（記為 empty，不會重抓）：")
            for url in self.empty:
                log.info("    - %s", url)
        if self.failed:
            log.warning("本次失敗的頁面（記為 error，下次會重試）：")
            for url, reason in self.failed:
                log.warning("    - %s\n        原因：%s", url, reason)

    # ---------- 列表頁 ----------

    def _check_budget(self) -> None:
        if self.deadline and time.monotonic() >= self.deadline:
            raise _Budget("已達時間上限")

    def _listing_pages(self, start_url: str) -> Iterator[tuple[str, BeautifulSoup]]:
        pg = self.cfg.listing.pagination
        max_pages = pg.max_pages or 0

        cursor = None if self.restart else self.db.get_cursor(self.cfg.name, start_url)

        if pg.url_template:
            # 這個模式的游標存的是頁碼
            page = int(cursor) if (cursor or "").isdigit() else pg.start
            if cursor:
                log.info("從上次的第 %d 頁繼續", page)
            count = 0
            while True:
                self._check_budget()
                if pg.end is not None and page > pg.end:
                    return
                if max_pages and count >= max_pages:
                    return
                url = pg.url_template.format(page=page)
                soup = self._get_soup(url)
                if soup is None:
                    return
                yield url, soup
                self._save_cursor(start_url, str(page))
                page += pg.step
                count += 1
            return

        url: str | None = cursor or start_url
        if cursor:
            log.info("從上次中斷的列表頁繼續：%s", cursor)
        count = 0
        seen: set[str] = set()
        while url:
            self._check_budget()
            if max_pages and count >= max_pages:
                return
            if url in seen:  # 「下一頁」指回自己就停
                return
            seen.add(url)
            soup = self._get_soup(url)
            if soup is None:
                return
            yield url, soup
            # 這一頁的項目都處理完了才推進游標
            self._save_cursor(start_url, url)
            count += 1
            url = self._next_page_url(soup, url) if pg.next_page else None

    def _save_cursor(self, start_url: str, value: str) -> None:
        if self.dry_run:
            return
        self.db.set_cursor(self.cfg.name, start_url, value)
        self.db.commit()

    def _next_page_url(self, soup: BeautifulSoup, base_url: str) -> str | None:
        pg = self.cfg.listing.pagination
        links = extract_links(soup, pg.next_page or "", "href", base_url)
        for link in links:
            if self.cfg.in_scope(link):
                return link
        return None

    def _crawl_listing(self, start_url: str) -> None:
        listing = self.cfg.listing

        for page_url, soup in self._listing_pages(start_url):
            if listing.fields_on_listing:
                # 列表頁上就有全部欄位，不必進詳細頁
                roots = soup.select(listing.item) if listing.item else [soup]
                if not roots:
                    log.info("列表頁沒有項目，停止：%s", page_url)
                    return
                for root in roots:
                    data = extract_item(root, self.cfg.detail.fields, page_url, page_root=soup)
                    self._save(page_url, data)
                continue

            if not listing.item_link:
                # 沒有列表設定 → start_urls 本身就是詳細頁
                self._crawl_detail(page_url)
                continue

            scope = soup.select(listing.item) if listing.item else [soup]
            links: list[str] = []
            for node in scope:
                for link in extract_links(node, listing.item_link, listing.link_attr, page_url):
                    if self.cfg.in_scope(link) and link not in links:
                        links.append(link)

            if not links:
                log.info("列表頁抽不到項目連結，停止翻頁：%s", page_url)
                return

            log.info("列表頁 %s → %d 個項目", page_url, len(links))
            for link in links:
                self._crawl_detail(link)

    # ---------- 詳細頁 ----------

    def _crawl_detail(self, url: str) -> None:
        self._check_budget()
        if not self.force and self.db.is_done(url):
            self.skipped += 1
            log.debug("已抓過，跳過：%s", url)
            return

        soup = self._get_soup(url, record_state=True)
        if soup is None:
            return

        roots = soup.select(self.cfg.detail.item) if self.cfg.detail.item else [soup]
        if not roots:
            log.warning("詳細頁找不到項目區塊：%s", url)

        got = 0
        for root in roots:
            data = extract_item(root, self.cfg.detail.fields, url, page_root=soup)
            got += self._save(url, data)

        if not got:
            # 抽不到圖片（公告頁、隱私權頁之類）不算失敗，記成 empty 並視同處理完，
            # 下次重跑不會再訪問一次
            self.empty.append(url)
            log.info("沒有圖片，記為 empty：%s", url)

        if not self.dry_run:
            self.db.mark(url, self.cfg.name, "done" if got else "empty")
            if self.saved % 20 == 0:
                self.db.commit()

    # ---------- 存檔 ----------

    def _save(self, page_url: str, data: dict[str, Any]) -> int:
        image_urls = _as_list(data.get("image_url"))
        if not image_urls:
            log.warning("抽不到 image_url：%s", page_url)
            return 0

        name = _as_text(data.get("name"))
        description = _as_text(data.get("description"))
        published_at = _as_text(data.get("published_at"))
        tags = self._normalize_tags(data.get("tags"))
        html_w = _as_int(data.get("width"))
        html_h = _as_int(data.get("height"))

        count = 0
        for image_url in image_urls:
            if not image_url.lower().startswith(("http://", "https://")):
                log.warning("略過非 http 圖片網址：%s", image_url)
                continue

            width, height = html_w, html_h
            mode = self.cfg.measure_size
            if mode == "always" or (mode == "missing" and not (width and height)):
                size = self.fetcher.image_size(image_url, referer=page_url)
                if size:
                    width, height = size

            rec = ImageRecord(
                site=self.cfg.name,
                page_url=page_url,
                image_url=image_url,
                name=name,
                description=description,
                width=width,
                height=height,
                published_at=published_at,
                tags=tags,
            )

            if self.dry_run:
                log.info(
                    "[dry-run] %s | %sx%s | %s | tags=%s | %s\n           %s",
                    name, width, height, published_at, tags,
                    (description or "")[:50], image_url,
                )
            else:
                self.db.upsert_image(rec)

            self.saved += 1
            count += 1
            if self.limit and self.saved >= self.limit:
                if not self.dry_run:
                    self.db.mark(page_url, self.cfg.name, "done")
                    self.db.commit()
                raise _Budget(f"已達筆數上限 {self.limit}")

        return count

    def _normalize_tags(self, value: Any) -> list[str]:
        tags = _as_list(value)
        sep = self.cfg.tag_separator
        if sep:
            pattern = "[" + re.escape(sep) + "]"
            tags = [part for tag in tags for part in re.split(pattern, tag)]
        return sorted({t.strip() for t in tags if t and t.strip()})

    # ---------- 工具 ----------

    def _get_soup(self, url: str, record_state: bool = False) -> BeautifulSoup | None:
        try:
            html = self.fetcher.get_html(url)
        except Exception as exc:
            self.errors += 1
            self.failed.append((url, f"{type(exc).__name__}: {exc}"))
            log.error("取得失敗 %s：%s", url, exc)
            if record_state and not self.dry_run:
                self.db.mark(url, self.cfg.name, "error", str(exc))
                self.db.commit()
            return None
        return parse_html(html)
