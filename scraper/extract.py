"""把 FieldSpec 套用到 BeautifulSoup 節點，取出欄位值。"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from dateutil import parser as dateparser

from .config import URL_ATTRS, FieldSpec

log = logging.getLogger(__name__)

_WS = re.compile(r"\s+")
# selector 寫成 "." 代表「目前這個區塊自己」，而不是它的後代
SELF_SELECTOR = "."
# 中日文常見寫法：2026年7月30日 / 2026年7月30日 12:34
_CJK_DATE = re.compile(r"(\d{4})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})\s*日?")


def parse_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _clean(text: str, strip: bool) -> str:
    text = _WS.sub(" ", text)
    return text.strip() if strip else text


def _node_value(node: Tag, spec: FieldSpec, base_url: str) -> str | None:
    if spec.attr == "text":
        value = node.get_text(separator=spec.separator)
    elif spec.attr == "html":
        value = node.decode_contents()
    else:
        raw = node.get(spec.attr)
        if raw is None:
            return None
        value = " ".join(raw) if isinstance(raw, list) else str(raw)
        if spec.attr == "srcset":  # 取第一個候選網址
            value = value.split(",")[0].strip().split(" ")[0]

    value = _clean(value, spec.strip)
    if not value:
        return None

    if spec.regex:
        m = re.search(spec.regex, value, re.S)
        if not m:
            return None
        value = (m.group(spec.regex_group) or "").strip()
        if not value:
            return None

    if spec.type == "url" or spec.attr in URL_ATTRS:
        value = urljoin(base_url, value)

    return value


def _to_date(value: str) -> str | None:
    """回傳 ISO 8601；只有日期就回 YYYY-MM-DD。"""
    m = _CJK_DATE.search(value)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            from datetime import date

            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    try:
        dt = dateparser.parse(value)
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None:
        return None
    if dt.hour or dt.minute or dt.second:
        return dt.isoformat()
    return dt.date().isoformat()


def _convert(value: str, spec: FieldSpec) -> Any:
    if spec.type == "int":
        m = re.search(r"-?\d+", value.replace(",", ""))
        return int(m.group()) if m else None
    if spec.type == "date":
        return _to_date(value)
    return value


def extract_field(
    root: Tag, chain: list[FieldSpec], base_url: str, page_root: Tag | None = None
) -> Any:
    """依序套用規則鏈，第一條有值的就採用。

    scope='page' 的規則會改從整份文件找，讓「一頁多圖、但說明／日期／標籤共用」
    的版型也能正確抽值。
    """
    for spec in chain:
        if spec.const is not None:
            return spec.const
        if not spec.selector:
            continue

        search_root = page_root if (spec.scope == "page" and page_root is not None) else root
        nodes = (
            [search_root]
            if spec.selector == SELF_SELECTOR
            else search_root.select(spec.selector)
        )

        if spec.multiple:
            values = []
            for node in nodes:
                raw = _node_value(node, spec, base_url)
                if raw is None:
                    continue
                converted = _convert(raw, spec)
                if converted is not None and converted not in values:
                    values.append(converted)
            if values:
                return values
            continue

        # 取第一個「真的有值」的節點；版型裡常有空殼元素排在前面
        for node in nodes:
            raw = _node_value(node, spec, base_url)
            if raw is None:
                continue
            converted = _convert(raw, spec)
            if converted is not None:
                return converted

    for spec in chain:
        if spec.required:
            raise ValueError(f"必填欄位抽不到值：selector={spec.selector!r}")
    return chain[0].default if chain else None


def extract_item(
    root: Tag,
    fields: dict[str, list[FieldSpec]],
    base_url: str,
    page_root: Tag | None = None,
) -> dict[str, Any]:
    return {
        name: extract_field(root, chain, base_url, page_root)
        for name, chain in fields.items()
    }


def extract_links(root: Tag, selector: str, attr: str, base_url: str) -> list[str]:
    """抽出頁面上的連結，保序去重。"""
    seen: dict[str, None] = {}
    for node in root.select(selector):
        raw = node.get(attr)
        if not raw:
            continue
        url = urljoin(base_url, str(raw).strip())
        seen.setdefault(url, None)
    return list(seen)
