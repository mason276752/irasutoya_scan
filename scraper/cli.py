"""命令列介面：crawl / search / stats / tags / export。"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys

from .config import SiteConfig
from .crawl import Crawler
from .db import Database
from .fetch import Fetcher


def _setup_logging(verbose: bool, log_file: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:  # CI 上把完整 log 留成檔案，方便當成 artifact 下載
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def cmd_crawl(args: argparse.Namespace) -> int:
    cfg = SiteConfig.load(args.config)

    # 命令列可覆寫速度設定，方便臨時調整
    if args.delay is not None:
        cfg.politeness.delay = args.delay
    if args.jitter is not None:
        cfg.politeness.jitter = args.jitter
    if args.max_pages is not None:
        cfg.listing.pagination.max_pages = args.max_pages
    if args.no_robots:
        cfg.politeness.respect_robots = False

    logging.info(
        "站台 %s｜間隔 %.1fs（抖動 %.1fs）｜robots %s｜尺寸量測 %s",
        cfg.name,
        cfg.politeness.delay,
        cfg.politeness.jitter,
        "遵守" if cfg.politeness.respect_robots else "忽略",
        cfg.measure_size,
    )

    db = Database(args.db)
    crawler = Crawler(
        cfg,
        db,
        fetcher=Fetcher(cfg.politeness),
        limit=args.limit or 0,
        force=args.force,
        dry_run=args.dry_run,
        max_runtime=args.max_runtime or 0,
        restart=args.restart,
    )
    crawler.run()
    stats = db.stats()
    db.close()
    logging.info("資料庫現況：%s", stats)
    return 0 if crawler.errors == 0 else 1


def cmd_retry(args: argparse.Namespace) -> int:
    cfg = SiteConfig.load(args.config)
    db = Database(args.db)
    urls = [row["url"] for row in db.pages_by_status("error", cfg.name)]
    if not urls:
        logging.info("沒有需要重試的頁面")
        db.close()
        return 0

    logging.info("重試 %d 個失敗頁面", len(urls))
    crawler = Crawler(cfg, db, limit=args.limit or 0, max_runtime=args.max_runtime or 0)
    crawler.crawl_urls(urls)
    db.close()
    return 0 if crawler.errors == 0 else 1


def cmd_errors(args: argparse.Namespace) -> int:
    db = Database(args.db)
    rows = db.pages_by_status(args.status, args.site)
    for row in rows:
        print(f"{row['updated_at']}  {row['url']}")
        if row["error"]:
            print(f"    原因：{row['error']}")
    label = "失敗" if args.status == "error" else "無圖片"
    print(f"\n共 {len(rows)} 個{label}頁面")
    db.close()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    db = Database(args.db)
    rows = db.search(
        tags=args.tag or (),
        match_any=args.any,
        query=args.q,
        raw_query=args.raw,
        site=args.site,
        since=args.since,
        until=args.until,
        limit=args.limit,
        offset=args.offset,
    )
    if args.json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        for row in rows:
            size = f"{row['width']}x{row['height']}" if row["width"] else "尺寸未知"
            print(f"[{row['id']}] {row['name'] or '(無名稱)'}  {size}  {row['published_at'] or ''}")
            print(f"    {row['image_url']}")
            if row["tags"]:
                print(f"    標籤：{'、'.join(row['tags'])}")
        print(f"\n共 {len(rows)} 筆")
    db.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    db = Database(args.db)
    stats = db.stats()
    print(f"圖片：{stats['images']}")
    print(f"標籤：{stats['tags']}")
    print(f"有尺寸：{stats['with_size']}")
    print(f"已完成頁面：{stats['pages_done']}，無圖片頁面：{stats['pages_empty']}，"
          f"失敗頁面：{stats['pages_error']}")
    print(f"全文索引：{'啟用' if stats['fts'] else '未啟用（搜尋改用 LIKE）'}")
    db.close()
    return 0


def cmd_tags(args: argparse.Namespace) -> int:
    db = Database(args.db)
    for name, count in db.top_tags(args.limit):
        print(f"{count:6d}  {name}")
    db.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    db = Database(args.db)
    rows = db.search(limit=args.limit or 10**9)
    out = open(args.out, "w", encoding="utf-8", newline="") if args.out else sys.stdout
    try:
        if args.format == "json":
            json.dump(rows, out, ensure_ascii=False, indent=2)
            out.write("\n")
        else:
            writer = csv.writer(out)
            writer.writerow(
                ["id", "site", "name", "description", "width", "height",
                 "image_url", "page_url", "published_at", "tags"]
            )
            for r in rows:
                writer.writerow([
                    r["id"], r["site"], r["name"], r["description"],
                    r["width"], r["height"], r["image_url"], r["page_url"],
                    r["published_at"], "|".join(r["tags"]),
                ])
    finally:
        if args.out:
            out.close()
    db.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scraper", description="通用 HTML 圖片 metadata 爬蟲")
    p.add_argument("-v", "--verbose", action="store_true", help="輸出除錯訊息")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("crawl", help="依設定檔爬取")
    c.add_argument("-c", "--config", required=True, help="站台設定檔（YAML）")
    c.add_argument("-d", "--db", default="data/images.db", help="SQLite 檔案路徑")
    c.add_argument("--limit", type=int, default=0, help="最多寫入幾筆後停止（0=不限）")
    c.add_argument("--max-pages", type=int, default=None, help="最多翻幾頁列表")
    c.add_argument("--delay", type=float, default=None, help="覆寫請求間隔秒數")
    c.add_argument("--jitter", type=float, default=None, help="覆寫隨機抖動秒數")
    c.add_argument("--max-runtime", type=float, default=None,
                   help="跑滿幾秒就收工（CI 有時間上限時用，進度會保存）")
    c.add_argument("--restart", action="store_true",
                   help="忽略上次的翻頁進度，從 start_urls 重新開始")
    c.add_argument("--force", action="store_true", help="已抓過的頁面也重抓")
    c.add_argument("--dry-run", action="store_true", help="只印出結果，不寫入資料庫")
    c.add_argument("--no-robots", action="store_true", help="不檢查 robots.txt")
    c.add_argument("--log-file", help="同時把 log 寫到檔案")
    c.set_defaults(func=cmd_crawl)

    r = sub.add_parser("retry", help="重跑先前失敗（error）的頁面")
    r.add_argument("-c", "--config", required=True)
    r.add_argument("-d", "--db", default="data/images.db")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--max-runtime", type=float, default=None)
    r.add_argument("--log-file")
    r.set_defaults(func=cmd_retry)

    er = sub.add_parser("errors", help="列出失敗／無圖片的頁面")
    er.add_argument("-d", "--db", default="data/images.db")
    er.add_argument("--status", choices=["error", "empty"], default="error")
    er.add_argument("--site")
    er.set_defaults(func=cmd_errors)

    s = sub.add_parser("search", help="查詢資料庫")
    s.add_argument("-d", "--db", default="data/images.db")
    s.add_argument("-t", "--tag", action="append", help="標籤（可重複；預設全部符合）")
    s.add_argument("--any", action="store_true", help="標籤改成符合任一個")
    s.add_argument("-q", help="名稱／說明全文搜尋（短於 3 字自動改用 LIKE）")
    s.add_argument("--raw", action="store_true", help="-q 直接當成 FTS5 語法（AND/OR/NEAR/*）")
    s.add_argument("--site", help="限定站台")
    s.add_argument("--since", help="公開日期下界（YYYY-MM-DD）")
    s.add_argument("--until", help="公開日期上界（YYYY-MM-DD）")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--json", action="store_true", help="輸出 JSON")
    s.set_defaults(func=cmd_search)

    st = sub.add_parser("stats", help="顯示資料庫統計")
    st.add_argument("-d", "--db", default="data/images.db")
    st.set_defaults(func=cmd_stats)

    t = sub.add_parser("tags", help="列出最常見的標籤")
    t.add_argument("-d", "--db", default="data/images.db")
    t.add_argument("--limit", type=int, default=30)
    t.set_defaults(func=cmd_tags)

    e = sub.add_parser("export", help="匯出 JSON / CSV")
    e.add_argument("-d", "--db", default="data/images.db")
    e.add_argument("--format", choices=["json", "csv"], default="json")
    e.add_argument("--out", help="輸出檔案（預設印到 stdout）")
    e.add_argument("--limit", type=int, default=0)
    e.set_defaults(func=cmd_export)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, getattr(args, "log_file", None))
    return args.func(args)
