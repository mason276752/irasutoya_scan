"""HTTP 取得層：限速、重試、robots.txt、串流量測圖片尺寸。

單執行緒、一次一個請求。速度控制是這支爬蟲的硬性要求，不做並發。
"""

from __future__ import annotations

import io
import logging
import random
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from PIL import Image, ImageFile

from .config import Politeness

log = logging.getLogger(__name__)

# 量測尺寸時最多讀這麼多位元組就放棄，避免整張圖被拉下來
MAX_SIZE_PROBE_BYTES = 256 * 1024
PROBE_CHUNK = 8 * 1024


class RateLimiter:
    """確保任兩次請求之間至少間隔 delay 秒，另加 0~jitter 的隨機抖動。"""

    def __init__(self, delay: float, jitter: float = 0.0):
        self.delay = max(0.0, delay)
        self.jitter = max(0.0, jitter)
        self._last = 0.0

    def wait(self) -> None:
        target = self.delay + random.uniform(0.0, self.jitter)
        elapsed = time.monotonic() - self._last
        if elapsed < target:
            time.sleep(target - elapsed)
        self._last = time.monotonic()


class Fetcher:
    def __init__(self, politeness: Politeness):
        self.p = politeness
        self.limiter = RateLimiter(politeness.delay, politeness.jitter)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": politeness.user_agent,
                "Accept-Language": "ja,zh-TW;q=0.9,en;q=0.8",
                **politeness.headers,
            }
        )
        self._robots: dict[str, RobotFileParser | None] = {}

    # ---------- robots ----------

    def allowed(self, url: str) -> bool:
        if not self.p.respect_robots:
            return True
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
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

    # ---------- 請求 ----------

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """帶重試與指數退避；429/503 會尊重 Retry-After。"""
        last_exc: Exception | None = None
        for attempt in range(self.p.retries + 1):
            self.limiter.wait()
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
                wait = float(retry_after) if (retry_after or "").isdigit() else self.p.backoff**attempt
                last_exc = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                if attempt < self.p.retries:
                    log.warning("HTTP %d（%s），%.1fs 後重試", resp.status_code, url, wait)
                    time.sleep(wait)
                    continue

            resp.raise_for_status()
            return resp

        raise last_exc or RuntimeError(f"無法取得 {url}")

    def get_html(self, url: str) -> str:
        if not self.allowed(url):
            raise PermissionError(f"robots.txt 不允許抓取：{url}")
        resp = self._request("GET", url)
        # 讓 requests 依 HTML meta 猜編碼，避免日文頁被判成 ISO-8859-1
        if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding
        return resp.text

    # ---------- 圖片尺寸 ----------

    def image_size(self, url: str, referer: str | None = None) -> tuple[int, int] | None:
        """實際連線讀圖片，讀到能判斷尺寸就中斷，不會下載整張圖。"""
        if not self.allowed(url):
            log.warning("robots.txt 不允許量測圖片：%s", url)
            return None
        headers = {"Referer": referer} if referer else {}
        try:
            resp = self._request("GET", url, headers=headers, stream=True)
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
