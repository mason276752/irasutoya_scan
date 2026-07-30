"""HTTP 取得層：限速、重試、robots.txt、串流量測圖片尺寸。

單執行緒、一次一個請求。速度控制是這支爬蟲的硬性要求，不做並發。
"""

from __future__ import annotations

import io
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from PIL import Image, ImageFile

from .config import Politeness

log = logging.getLogger(__name__)


class TimeBudgetExceeded(Exception):
    """等待 429 冷卻的過程中超出時間預算，必須乾淨收工而不是睡完。"""

# 量測尺寸時最多讀這麼多位元組就放棄，避免整張圖被拉下來
MAX_SIZE_PROBE_BYTES = 256 * 1024
PROBE_CHUNK = 8 * 1024

# 瀏覽器導覽網頁時會送的標頭。
# 刻意不指定 Accept-Encoding —— 交給 requests 依實際安裝的解碼器決定，
# 硬寫 br 而環境沒有 brotli 會拿到解不開的內容。
PAGE_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
}

# 瀏覽器載入 <img> 時送的標頭，跟導覽網頁不一樣
IMAGE_HEADERS = {
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Sec-Ch-Ua": PAGE_HEADERS["Sec-Ch-Ua"],
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "image",
    "Sec-Fetch-Mode": "no-cors",
}


class RateLimiter:
    """確保任兩次請求之間至少間隔 delay 秒，另加 0~jitter 的隨機抖動。

    圖片量測是多執行緒的，所以整段等待都在鎖內，讓間隔對所有執行緒一致生效。
    delay 與 jitter 都是 0 時完全不鎖，避免白白序列化。
    """

    def __init__(self, delay: float, jitter: float = 0.0):
        self.delay = max(0.0, delay)
        self.jitter = max(0.0, jitter)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if not self.delay and not self.jitter:
            return
        with self._lock:
            gap = self.delay + random.uniform(0.0, self.jitter)
            elapsed = time.monotonic() - self._last
            if elapsed < gap:
                time.sleep(gap - elapsed)
            self._last = time.monotonic()


class Fetcher:
    def __init__(self, politeness: Politeness):
        self.p = politeness
        self.limiter = RateLimiter(politeness.delay, politeness.jitter)  # 網頁
        self.image_limiter = RateLimiter(politeness.image_delay)  # 圖片
        self._headers = {
            "User-Agent": politeness.user_agent,
            "Accept-Language": politeness.accept_language,
            **(PAGE_HEADERS if politeness.browser_headers else {}),
            **politeness.headers,  # 設定檔的自訂標頭優先度最高
        }
        self._local = threading.local()  # 每個執行緒各自的 Session
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        # 被 429 擋下時的冷卻，以 host 為單位：
        # 限流是伺服器層級的，同一台主機上的網頁與圖片必須一起停，
        # 但不該波及其他主機（例如圖片放在另一個 CDN 的情況）。
        self._cooldown: dict[str, float] = {}
        self._cooldown_lock = threading.Lock()
        # 由 Crawler 設定的收工時間點（time.monotonic 基準）。
        # 冷卻動輒一分鐘起跳，等待期間必須看得到時間上限，否則會遠遠超時。
        self.deadline: float | None = None

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self._headers)
            self._local.session = session
        return session

    # ---------- robots ----------

    def allowed(self, url: str) -> bool:
        if not self.p.respect_robots:
            return True
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        with self._robots_lock:  # 多執行緒量測圖片時，同一個 host 只載入一次
            if root not in self._robots:
                self._robots[root] = self._load_robots(root)
            rp = self._robots[root]
        if rp is None:  # 拿不到 robots.txt 就當作沒有限制
            return True
        return rp.can_fetch(self.p.user_agent, url)

    def _load_robots(self, root: str) -> RobotFileParser | None:
        url = urljoin(root, "/robots.txt")
        try:
            self.limiter.wait()
            resp = self.session.get(url, timeout=self.p.timeout)
            if resp.status_code != 200:
                return None
            rp = RobotFileParser()
            rp.parse(resp.text.splitlines())
            log.info("已載入 robots.txt：%s", url)
            return rp
        except requests.RequestException as exc:
            log.warning("讀不到 robots.txt（%s）：%s", url, exc)
            return None

    # ---------- 429 冷卻（以 host 為單位）----------

    def set_cooldown(self, url: str, seconds: float) -> None:
        host = urlparse(url).hostname or ""
        with self._cooldown_lock:
            until = max(self._cooldown.get(host, 0.0), time.monotonic() + seconds)
            self._cooldown[host] = until

    def wait_cooldown(self, url: str) -> None:
        """這台主機若在冷卻中就等到結束。

        分段睡，好讓等待中途也能察覺時間上限已到 —— 冷卻可能長達一分鐘，
        睡完才發現超時的話，收工時間會嚴重失準。
        """
        host = urlparse(url).hostname or ""
        while True:
            with self._cooldown_lock:
                until = self._cooldown.get(host, 0.0)
            remain = until - time.monotonic()
            if remain <= 0:
                return
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise TimeBudgetExceeded(
                    f"等待 {host} 的 429 冷卻（還要 {remain:.0f} 秒）期間已達時間上限"
                )
            time.sleep(min(remain, 1.0))

    # ---------- 請求 ----------

    def _request(
        self, method: str, url: str, limiter: RateLimiter | None = None, **kwargs
    ) -> requests.Response:
        """帶重試；429 觸發全域冷卻，5xx 用指數退避。兩者都尊重 Retry-After。"""
        limiter = limiter or self.limiter
        last_exc: Exception | None = None
        for attempt in range(self.p.retries + 1):
            self.wait_cooldown(url)  # 這台主機還在 429 冷卻中就先等
            limiter.wait()
            try:
                resp = self.session.request(
                    method, url, timeout=self.p.timeout, **kwargs
                )
            except requests.RequestException as exc:
                last_exc = exc
                wait = self.p.backoff**attempt
                log.warning("請求失敗（%s）第 %d 次：%s，%.1fs 後重試", url, attempt + 1, exc, wait)
                time.sleep(wait)
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                server_wait = float(retry_after) if (retry_after or "").isdigit() else 0.0
                last_exc = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)

                if resp.status_code == 429:
                    # 被限流：讓整台主機冷卻，網頁與並行中的圖片請求都會停下來。
                    # 冷卻要在放棄重試之前就設好 —— 這一個請求就算不再重試，
                    # 其他執行緒和後續請求仍然必須受到保護。
                    wait = max(server_wait, self.p.too_many_requests_wait)
                    self.set_cooldown(url, wait)
                    log.warning(
                        "HTTP 429（%s），%s 全站暫停 %.0f 秒",
                        url, urlparse(url).hostname, wait,
                    )
                    if attempt >= self.p.retries:
                        break
                    # 不用自己 sleep —— 下一輪開頭的 wait_cooldown 會等滿
                    continue

                if attempt >= self.p.retries:
                    break
                wait = server_wait or self.p.backoff**attempt
                log.warning("HTTP %d（%s），%.1fs 後重試", resp.status_code, url, wait)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp

        raise last_exc or RuntimeError(f"無法取得 {url}")

    def _fetch_site(self, url: str, referer: str | None) -> str:
        """Sec-Fetch-Site：瀏覽器會依來源與目標的關係填不同的值。"""
        if not referer:
            return "none"
        return (
            "same-origin"
            if urlparse(url).hostname == urlparse(referer).hostname
            else "cross-site"
        )

    def _extra_headers(self, url: str, referer: str | None, image: bool) -> dict[str, str]:
        if not self.p.browser_headers:
            return {"Referer": referer} if referer else {}
        headers = dict(IMAGE_HEADERS) if image else {}
        headers["Sec-Fetch-Site"] = self._fetch_site(url, referer)
        if referer:
            headers["Referer"] = referer
        return headers

    def get_html(self, url: str, referer: str | None = None) -> str:
        if not self.allowed(url):
            raise PermissionError(f"robots.txt 不允許抓取：{url}")
        resp = self._request("GET", url, headers=self._extra_headers(url, referer, image=False))
        # 讓 requests 依 HTML meta 猜編碼，避免日文頁被判成 ISO-8859-1
        if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding
        return resp.text

    # ---------- 圖片尺寸 ----------

    def image_sizes(
        self, urls: list[str], referer: str | None = None
    ) -> dict[str, tuple[int, int] | None]:
        """並行量測多張圖的尺寸（同時最多 image_concurrency 張）。

        只有圖片走並行；網頁一律由呼叫端序列取得。
        """
        workers = min(self.p.image_concurrency, len(urls))
        if workers <= 1:
            return {url: self.image_size(url, referer) for url in urls}

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="imgsize") as pool:
            results = pool.map(lambda u: self.image_size(u, referer), urls)
            return dict(zip(urls, results))

    def image_size(self, url: str, referer: str | None = None) -> tuple[int, int] | None:
        """實際連線讀圖片，讀到能判斷尺寸就中斷，不會下載整張圖。"""
        if not self.allowed(url):
            log.warning("robots.txt 不允許量測圖片：%s", url)
            return None
        headers = self._extra_headers(url, referer, image=True)
        try:
            resp = self._request(
                "GET", url, limiter=self.image_limiter, headers=headers, stream=True
            )
        except TimeBudgetExceeded:
            raise  # 收工訊號不能被當成「這張圖量不到」吞掉
        except Exception as exc:
            log.warning("量測尺寸失敗（%s）：%s", url, exc)
            return None

        parser = ImageFile.Parser()
        buf = bytearray()
        try:
            for chunk in resp.iter_content(PROBE_CHUNK):
                if not chunk:
                    continue
                buf.extend(chunk)
                parser.feed(chunk)
                if parser.image is not None:
                    return parser.image.size
                if len(buf) >= MAX_SIZE_PROBE_BYTES:
                    log.warning("讀了 %d bytes 仍判不出尺寸：%s", len(buf), url)
                    return None
        except Exception as exc:
            log.warning("解析圖片失敗（%s）：%s", url, exc)
            return None
        finally:
            resp.close()

        # 串流結束仍未判出（例如整張圖很小、格式需要完整檔頭）：整份再餵一次
        try:
            with Image.open(io.BytesIO(bytes(buf))) as im:
                return im.size
        except Exception:
            log.warning("讀完 %d bytes 仍判不出尺寸：%s", len(buf), url)
            return None
