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
from .fetch import Fetcher, TimeBudgetExceeded

log = logging.getLogger(__name__)


class _Budget(Exception):
    """達到筆數上限或時間上限時，用來乾淨地中止整趟爬取（進度已存好）。"""


# 列表頁偶爾會抽不到項目（頁面殘缺、站方臨時異常）。只有連續這麼多頁都抽不到，
# 才認定是抽取規則壞了而停下來；否則單一異常頁會讓游標永遠卡在那裡，
# 後面的頁面再也走不到。
EMPTY_LISTING_TOLERANCE = 3


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
        # 讓取得層在等待 429 冷卻時也看得到收工時間
        self.fetcher.deadline = self.deadline
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
        except (_Budget, TimeBudgetExceeded) as exc:
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
        except (_Budget, TimeBudgetExceeded) as exc:
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

        if self.dry_run:
            return

        # 游標只會往前走，越過的列表頁不會再訪問，所以待處理的頁面必須靠 retry 補。
        # 把資料庫裡累積的數量講清楚，避免問題被埋在某一次的 log 裡。
        pending_error = len(self.db.pages_by_status("error", self.cfg.name))
        pending_empty = len(self.db.pages_by_status("empty", self.cfg.name))
        if pending_error:
            log.warning(
                "資料庫累積 %d 個 error 頁面，用 `retry` 補：\n"
                "    python -m scraper retry -c <設定檔> -d <資料庫>",
                pending_error,
            )
        missing_size = len(self.db.images_without_size(self.cfg.name))
        if missing_size:
            log.warning(
                "有 %d 張圖沒量到尺寸（量測當下可能被 429 擋掉），用 `remeasure` 補：\n"
                "    python -m scraper remeasure -c <設定檔> -d <資料庫>",
                missing_size,
            )
        if pending_empty:
            log.info(
                "資料庫累積 %d 個 empty 頁面（抓過但沒有圖）。多數是公告頁之類，"
                "但版型變動或頁面殘缺也會落到這裡，偶爾用 `retry --status empty` 複查",
                pending_empty,
            )

    # ---------- 列表頁 ----------

    def _check_budget(self) -> None:
        if self.deadline and time.monotonic() >= self.deadline:
            raise _Budget("已達時間上限")

    def _listing_pages(self, start_url: str) -> Iterator[tuple[str, BeautifulSoup]]:
        pg = self.cfg.listing.pagination
        max_pages = pg.max_pages or 0

        cursor = None if self.restart else self.db.get_cursor(self.cfg.name, start_url)
        prev: str | None = None  # 上一個列表頁，當作 Referer

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
                soup = self._get_soup(url, referer=prev)
                if soup is None:
                    return
                yield url, soup
                self._save_cursor(start_url, str(page))
                prev = url
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
            # 翻頁時帶上一頁當 Referer
            soup = self._get_soup(url, referer=prev)
            if soup is None:
                return
            yield url, soup
            # 這一頁的項目都處理完了才推進游標
            self._save_cursor(start_url, url)
            prev = url
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
        blank_streak = 0  # 連續幾頁抽不到東西

        for page_url, soup in self._listing_pages(start_url):
            if listing.fields_on_listing:
                # 列表頁上就有全部欄位，不必進詳細頁
                roots = soup.select(listing.item) if listing.item else [soup]
                if not roots:
                    blank_streak += 1
                    if blank_streak >= EMPTY_LISTING_TOLERANCE:
                        log.warning(
                            "連續 %d 頁抽不到項目，停止翻頁（抽取規則可能失效）：%s",
                            blank_streak, page_url,
                        )
                        return
                    log.warning("列表頁沒有項目，先跳過繼續翻：%s", page_url)
                    continue
                blank_streak = 0
                self._process_items(page_url, roots, soup, label="列表頁項目")
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
                blank_streak += 1
                if blank_streak >= EMPTY_LISTING_TOLERANCE:
                    log.warning(
                        "連續 %d 頁抽不到項目連結，停止翻頁（抽取規則可能失效）：%s",
                        blank_streak, page_url,
                    )
                    return
                log.warning("列表頁抽不到項目連結，先跳過繼續翻：%s", page_url)
                continue

            blank_streak = 0

            # 先分流，好讓 log 說清楚這一頁到底有沒有事情要做 ——
            # 只印「N 個項目」的話，整頁都已抓過時看起來像卡住了
            pending: list[str] = []
            done_here = 0
            for link in links:
                if not self.force and self.db.is_done(link):
                    done_here += 1
                else:
                    pending.append(link)
            self.skipped += done_here

            if pending:
                log.info(
                    "列表頁 %s → %d 個項目（%d 個要爬，%d 個已抓過）",
                    page_url, len(links), len(pending), done_here,
                )
            else:
                log.info(
                    "列表頁 %s → %d 個項目全部抓過，直接翻下一頁", page_url, len(links)
                )

            for link in pending:
                self._crawl_detail(link, referer=page_url, check_done=False)

    # ---------- 詳細頁 ----------

    def _crawl_detail(
        self, url: str, referer: str | None = None, check_done: bool = True
    ) -> None:
        self._check_budget()
        # 從列表頁進來的已經篩選過了，不用再查一次（也避免 skipped 重複計數）
        if check_done and not self.force and self.db.is_done(url):
            self.skipped += 1
            log.debug("已抓過，跳過：%s", url)
            return

        # 帶上列表頁當 Referer，跟瀏覽器從列表點進來一致
        soup = self._get_soup(url, record_state=True, referer=referer)
        if soup is None:
            return

        roots = soup.select(self.cfg.detail.item) if self.cfg.detail.item else [soup]
        if not roots:
            log.warning("詳細頁找不到項目區塊：%s", url)

        got = self._process_items(url, roots, soup, label="詳細頁")

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

    def _process_items(self, page_url: str, roots: list, page_soup, label: str = "頁面") -> int:
        """一個頁面上的所有項目：先全部抽好欄位，再一次並行量測所有圖片尺寸，最後寫入。

        量測批次要放在頁面層級 —— 設定檔常把「每張圖」當成一個 item
        （例如 detail.item 指到 <a>），若在 item 內量測，每次只有一張圖，並行等於沒開。
        """
        prepared = [
            self._prepare(page_url, extract_item(root, self.cfg.detail.fields, page_url, page_soup))
            for root in roots
        ]
        prepared = [p for p in prepared if p is not None]

        title = page_soup.title.get_text(strip=True) if page_soup.title else "(無標題)"
        total = sum(len(p["urls"]) for p in prepared)
        log.info("%s %s → %d 張圖\n           %s", label, title, total, page_url)

        if not prepared:
            return 0

        # 跨項目收集待量測的圖，保序去重後一次打完
        need = list(dict.fromkeys(url for item in prepared for url in item["need_measure"]))
        sizes = self.fetcher.image_sizes(need, referer=page_url) if need else {}
        if need:
            ok = sum(1 for v in sizes.values() if v)
            log.debug("量測 %d 張圖（成功 %d）：%s", len(need), ok, page_url)

        return sum(self._persist(page_url, item, sizes) for item in prepared)

    def _prepare(self, page_url: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """抽好的欄位 → 待寫入的資料，並算出哪些圖需要連線量測。"""
        image_urls = _as_list(data.get("image_url"))
        if not image_urls:
            log.warning("抽不到 image_url：%s", page_url)
            return None

        valid: list[str] = []
        for image_url in image_urls:
            if image_url.lower().startswith(("http://", "https://")):
                valid.append(image_url)
            else:
                log.warning("略過非 http 圖片網址：%s", image_url)
        if not valid:
            return None

        html_w = _as_int(data.get("width"))
        html_h = _as_int(data.get("height"))
        mode = self.cfg.measure_size
        need = valid if mode == "always" or (mode == "missing" and not (html_w and html_h)) else []

        return {
            "urls": valid,
            "need_measure": need,
            "name": _as_text(data.get("name")),
            "description": _as_text(data.get("description")),
            "published_at": _as_text(data.get("published_at")),
            "tags": self._normalize_tags(data.get("tags")),
            "width": html_w,
            "height": html_h,
        }

    def _persist(
        self, page_url: str, item: dict[str, Any], sizes: dict[str, tuple[int, int] | None]
    ) -> int:
        name = item["name"]
        description = item["description"]
        published_at = item["published_at"]
        tags = item["tags"]

        count = 0
        for image_url in item["urls"]:
            width, height = item["width"], item["height"]
            measured = sizes.get(image_url)
            if measured:
                width, height = measured

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
                # 這一頁還沒處理完就中斷，所以**不能**標記成 done
                # ——標記了下次就會跳過，該頁剩下的圖會永久漏掉。
                # 已寫入的圖先 commit 保住，整頁留待下次重抓（upsert 不會產生重複）。
                if not self.dry_run:
                    self.db.commit()
                raise _Budget(f"已達筆數上限 {self.limit}（這一頁未完成，下次會重抓）")

        return count

    def _normalize_tags(self, value: Any) -> list[str]:
        tags = _as_list(value)
        sep = self.cfg.tag_separator
        if sep:
            pattern = "[" + re.escape(sep) + "]"
            tags = [part for tag in tags for part in re.split(pattern, tag)]
        return sorted({t.strip() for t in tags if t and t.strip()})

    # ---------- 工具 ----------

    def _get_soup(
        self, url: str, record_state: bool = False, referer: str | None = None
    ) -> BeautifulSoup | None:
        try:
            html = self.fetcher.get_html(url, referer=referer)
        except TimeBudgetExceeded:
            # 收工訊號，不是這一頁的問題。要是被下面當成失敗記成 error，
            # 等於把「還沒處理」寫成「處理失敗」，進度記錄就不誠實了。
            raise
        except Exception as exc:
            self.errors += 1
            self.failed.append((url, f"{type(exc).__name__}: {exc}"))
            log.error("取得失敗 %s：%s", url, exc)
            if record_state and not self.dry_run:
                self.db.mark(url, self.cfg.name, "error", str(exc))
                self.db.commit()
            return None
        return parse_html(html)
