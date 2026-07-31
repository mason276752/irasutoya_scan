"""站台設定檔（YAML）→ dataclass。

設定檔用 CSS selector 描述「列表頁怎麼翻、詳細頁抽哪些元素」，
換一個站只要換一份 YAML，程式碼不用動。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

# 抽欄位時，這些屬性的值視為 URL，會自動補成絕對網址。
# content（og:image 之類）不在此列，因為它也常放非網址的值；需要時在設定檔寫 type: url。
URL_ATTRS = {"src", "href", "data-src", "data-original", "data-lazy-src", "srcset"}


@dataclass
class FieldSpec:
    """單一欄位的抽取規則。

    一個欄位可以掛多條規則（YAML 寫成 list），依序嘗試，第一條有值的就採用，
    用來對付同站不同版型的頁面。
    """

    selector: str | None = None
    attr: str = "text"  # text | html | 任意屬性名（src / href / datetime / content ...）
    # item = 只在目前項目區塊內找（預設）；page = 從整份文件找。
    # 一頁多張圖、但說明／日期／標籤共用時，那些欄位要設 page。
    scope: str = "item"
    multiple: bool = False  # True → 收集所有符合的元素，回傳 list
    regex: str | None = None  # 對抽到的字串再做一次擷取
    regex_group: int = 1
    type: str = "text"  # text | int | date | url
    default: Any = None
    required: bool = False
    separator: str = " "  # attr=text 時，元素內文字節點的接合字元
    strip: bool = True
    const: Any = None  # 不看 HTML，直接給固定值

    @classmethod
    def parse(cls, raw: Any) -> FieldSpec:
        if isinstance(raw, str):  # 只寫 selector 的簡寫
            return cls(selector=raw)
        if not isinstance(raw, dict):
            raise ValueError(f"欄位規則格式錯誤：{raw!r}")
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"欄位規則有未知的鍵：{sorted(unknown)}")
        return cls(**raw)


def parse_field_chain(raw: Any) -> list[FieldSpec]:
    """一個欄位 → 一條或多條規則。"""
    if isinstance(raw, list):
        return [FieldSpec.parse(item) for item in raw]
    return [FieldSpec.parse(raw)]


@dataclass
class Pagination:
    """兩種翻頁方式擇一：跟著「下一頁」連結走，或用網址模板套頁碼。"""

    next_page: str | None = None  # CSS selector，取 href
    url_template: str | None = None  # 例：https://example.com/list?page={page}
    start: int = 1
    end: int | None = None
    step: int = 1
    max_pages: int = 0  # 0 = 不限

    @classmethod
    def parse(cls, raw: Any) -> Pagination:
        if not raw:
            return cls()
        return cls(**raw)


@dataclass
class Listing:
    """列表頁：怎麼找到詳細頁連結、怎麼翻頁。"""

    item_link: str | None = None  # CSS selector，指向詳細頁的 <a>
    link_attr: str = "href"
    item: str | None = None  # 可選：先框出項目區塊，再從區塊內找連結
    pagination: Pagination = field(default_factory=Pagination)
    # 列表頁上就有全部欄位、不需要進詳細頁時設 True
    fields_on_listing: bool = False

    @classmethod
    def parse(cls, raw: Any) -> Listing:
        if not raw:
            return cls()
        raw = dict(raw)
        pagination = Pagination.parse(raw.pop("pagination", None))
        return cls(pagination=pagination, **raw)


@dataclass
class Detail:
    """詳細頁：頁內若有多個項目先用 item 框出來，再逐項抽 fields。"""

    item: str | None = None
    fields: dict[str, list[FieldSpec]] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> Detail:
        raw = dict(raw or {})
        fields_raw = raw.pop("fields", {}) or {}
        return cls(
            item=raw.pop("item", None),
            fields={k: parse_field_chain(v) for k, v in fields_raw.items()},
        )


# 一般桌機 Chrome 的 User-Agent。維持與其他標頭一致很重要 ——
# UA 說是 Chrome 卻少了 Sec-Fetch-* 這類標頭，反而比誠實的爬蟲 UA 更可疑。
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# 誠實表明身分的版本，把 browser_headers 設成 false 時使用
BOT_UA = "Mozilla/5.0 (compatible; StockLibraryScan/0.1) metadata-only crawler"


@dataclass
class Politeness:
    """爬蟲速度與禮貌設定。"""

    delay: float = 2.0  # 網頁請求之間至少間隔幾秒
    jitter: float = 1.0  # 額外隨機延遲 0~jitter 秒
    # 圖片只讀檔頭量尺寸，成本低，可以並行；網頁一律維持單執行緒序列
    image_concurrency: int = 10  # 同時最多幾張圖
    image_delay: float = 0.0  # 圖片請求之間的最小間隔（0 = 只靠並行數限制）
    timeout: float = 20.0
    retries: int = 3
    backoff: float = 2.0  # 5xx 的重試退避倍數
    # 被 429 擋下時暫停多久再試（秒）。這是全域冷卻，並行中的圖片請求也會一起停。
    too_many_requests_wait: float = 60.0
    # 送出跟一般瀏覽器一致的標頭組合（Accept、Sec-Fetch-*、Referer 等）。
    # 關掉的話會用 BOT_UA 並只送最基本的標頭。
    browser_headers: bool = True
    user_agent: str = ""  # 留空 = 依 browser_headers 自動選
    accept_language: str = "ja,en-US;q=0.9,en;q=0.8"
    respect_robots: bool = False
    headers: dict[str, str] = field(default_factory=dict)  # 自訂標頭，優先度最高

    def __post_init__(self) -> None:
        if not self.user_agent:
            self.user_agent = CHROME_UA if self.browser_headers else BOT_UA

    @classmethod
    def parse(cls, raw: Any) -> Politeness:
        return cls(**(raw or {}))


@dataclass
class SiteConfig:
    name: str
    start_urls: list[str]
    listing: Listing
    detail: Detail
    politeness: Politeness
    allowed_domains: list[str] = field(default_factory=list)
    max_items: int = 0  # 0 = 不限
    # 圖片尺寸：always=一律連線量測，missing=HTML 沒寫才量測，never=不量測
    measure_size: str = "missing"
    # 標籤抽出來是一整串字串時，用這些字元切開（None = 不切）
    tag_separator: str | None = None

    @classmethod
    def load(cls, path: str | Path) -> SiteConfig:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        start_urls = raw.get("start_urls") or []
        if isinstance(start_urls, str):
            start_urls = [start_urls]
        if not start_urls:
            raise ValueError(f"{path}: start_urls 不能是空的")

        allowed = raw.get("allowed_domains") or []
        if not allowed:  # 沒指定就鎖在起始網址的網域內
            allowed = sorted({h for u in start_urls if (h := urlparse(u).hostname)})

        measure = raw.get("measure_size", "missing")
        if measure not in {"always", "missing", "never"}:
            raise ValueError(f"measure_size 只能是 always / missing / never，收到 {measure!r}")

        return cls(
            name=raw.get("name") or Path(path).stem,
            start_urls=start_urls,
            listing=Listing.parse(raw.get("listing")),
            detail=Detail.parse(raw.get("detail")),
            politeness=Politeness.parse(raw.get("politeness")),
            allowed_domains=allowed,
            max_items=int(raw.get("max_items") or 0),
            measure_size=measure,
            tag_separator=raw.get("tag_separator"),
        )

    def in_scope(self, url: str) -> bool:
        host = urlparse(url).hostname  # 不含 port
        if not host:
            return False
        return any(host == d or host.endswith("." + d) for d in self.allowed_domains)
