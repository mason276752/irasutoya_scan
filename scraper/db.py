"""SQLite 儲存層。

標籤走正規化三表（images / tags / image_tags），因此標籤天然無序、不重複，
且「同時具備 A 和 B」這種查詢可以走索引，不必對逗號字串做 LIKE。
name / description 另外掛 FTS5 全文索引（優先用 trigram 分詞，對中日文子字串搜尋友善）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id           INTEGER PRIMARY KEY,
    site         TEXT    NOT NULL,
    page_url     TEXT    NOT NULL,
    image_url    TEXT    NOT NULL,
    name         TEXT,
    description  TEXT,
    width        INTEGER,
    height       INTEGER,
    published_at TEXT,               -- ISO 8601，無時間就存 YYYY-MM-DD
    fetched_at   TEXT    NOT NULL,
    UNIQUE (page_url, image_url)
);
CREATE INDEX IF NOT EXISTS idx_images_site      ON images (site);
CREATE INDEX IF NOT EXISTS idx_images_published ON images (published_at);
CREATE INDEX IF NOT EXISTS idx_images_name      ON images (name);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS image_tags (
    image_id INTEGER NOT NULL REFERENCES images (id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags (id)   ON DELETE CASCADE,
    PRIMARY KEY (image_id, tag_id)
) WITHOUT ROWID;
-- 反向索引：由標籤找圖
CREATE INDEX IF NOT EXISTS idx_image_tags_tag ON image_tags (tag_id, image_id);

-- 續爬用：記錄每個頁面的處理狀態
CREATE TABLE IF NOT EXISTS crawl_state (
    url        TEXT PRIMARY KEY,
    site       TEXT NOT NULL,
    status     TEXT NOT NULL,        -- done | empty（頁面沒有圖片）| error
    error      TEXT,
    updated_at TEXT NOT NULL
);

-- 列表翻頁的進度游標：記住最後處理完的列表頁，下次從那裡接著跑。
-- 一輪跑不完（CI 有時間上限）時靠它續跑；跑到最新一頁後，之後每次執行
-- 都從最新頁開始，只處理新增的內容。
CREATE TABLE IF NOT EXISTS crawl_cursor (
    site          TEXT NOT NULL,
    start_url     TEXT NOT NULL,
    last_page_url TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (site, start_url)
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS images_fts USING fts5 (
    name, description,
    content='images', content_rowid='id', tokenize={tokenize}
);
CREATE TRIGGER IF NOT EXISTS images_ai AFTER INSERT ON images BEGIN
    INSERT INTO images_fts (rowid, name, description)
    VALUES (new.id, new.name, new.description);
END;
CREATE TRIGGER IF NOT EXISTS images_ad AFTER DELETE ON images BEGIN
    INSERT INTO images_fts (images_fts, rowid, name, description)
    VALUES ('delete', old.id, old.name, old.description);
END;
CREATE TRIGGER IF NOT EXISTS images_au AFTER UPDATE ON images BEGIN
    INSERT INTO images_fts (images_fts, rowid, name, description)
    VALUES ('delete', old.id, old.name, old.description);
    INSERT INTO images_fts (rowid, name, description)
    VALUES (new.id, new.name, new.description);
END;
"""


@dataclass
class ImageRecord:
    site: str
    page_url: str
    image_url: str
    name: str | None = None
    description: str | None = None
    width: int | None = None
    height: int | None = None
    published_at: str | None = None
    tags: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.has_fts = False
        self._migrate()

    # ---------- schema ----------

    def _migrate(self) -> None:
        self.conn.executescript(SCHEMA)
        self.has_fts = self._try_fts()
        self.conn.commit()

    def _try_fts(self) -> bool:
        # trigram 需要 SQLite >= 3.34；不支援就退回 unicode61
        for tokenize in ("'trigram'", "'unicode61 remove_diacritics 2'"):
            try:
                self.conn.executescript(FTS_SCHEMA.format(tokenize=tokenize))
                return True
            except sqlite3.OperationalError:
                continue
        return False

    # ---------- 寫入 ----------

    def upsert_image(self, rec: ImageRecord) -> int:
        """以 (page_url, image_url) 為鍵寫入或更新，並同步標籤。"""
        cur = self.conn.execute(
            """
            INSERT INTO images
                (site, page_url, image_url, name, description,
                 width, height, published_at, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (page_url, image_url) DO UPDATE SET
                site         = excluded.site,
                name         = excluded.name,
                description  = excluded.description,
                -- 這次沒量到尺寸就保留舊值，不要用 NULL 蓋掉
                width        = COALESCE(excluded.width,  images.width),
                height       = COALESCE(excluded.height, images.height),
                published_at = COALESCE(excluded.published_at, images.published_at),
                fetched_at   = excluded.fetched_at
            RETURNING id
            """,
            (
                rec.site,
                rec.page_url,
                rec.image_url,
                rec.name,
                rec.description,
                rec.width,
                rec.height,
                rec.published_at,
                _now(),
            ),
        )
        image_id = int(cur.fetchone()[0])
        self._set_tags(image_id, rec.tags)
        return image_id

    def _set_tags(self, image_id: int, tags: Iterable[str]) -> None:
        clean = sorted({t.strip() for t in tags if t and t.strip()})
        if clean:
            self.conn.executemany(
                "INSERT OR IGNORE INTO tags (name) VALUES (?)", [(t,) for t in clean]
            )
            rows = self.conn.execute(
                f"SELECT id FROM tags WHERE name IN ({','.join('?' * len(clean))})", clean
            ).fetchall()
            tag_ids = [r["id"] for r in rows]
        else:
            tag_ids = []

        # 先刪掉這次不再出現的關聯，再補上新的（標籤集合以最新一次抓取為準）
        if tag_ids:
            self.conn.execute(
                f"DELETE FROM image_tags WHERE image_id = ? "
                f"AND tag_id NOT IN ({','.join('?' * len(tag_ids))})",
                [image_id, *tag_ids],
            )
            self.conn.executemany(
                "INSERT OR IGNORE INTO image_tags (image_id, tag_id) VALUES (?, ?)",
                [(image_id, tid) for tid in tag_ids],
            )
        else:
            self.conn.execute("DELETE FROM image_tags WHERE image_id = ?", (image_id,))

    def mark(self, url: str, site: str, status: str, error: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO crawl_state (url, site, status, error, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (url) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (url, site, status, error, _now()),
        )

    def is_done(self, url: str) -> bool:
        """done 與 empty 都算處理過；只有 error 會在下次重跑時重試。"""
        row = self.conn.execute(
            "SELECT 1 FROM crawl_state WHERE url = ? AND status IN ('done', 'empty')", (url,)
        ).fetchone()
        return row is not None

    def get_cursor(self, site: str, start_url: str) -> str | None:
        row = self.conn.execute(
            "SELECT last_page_url FROM crawl_cursor WHERE site = ? AND start_url = ?",
            (site, start_url),
        ).fetchone()
        return row["last_page_url"] if row else None

    def set_cursor(self, site: str, start_url: str, last_page_url: str | None) -> None:
        self.conn.execute(
            """
            INSERT INTO crawl_cursor (site, start_url, last_page_url, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (site, start_url) DO UPDATE SET
                last_page_url = excluded.last_page_url,
                updated_at = excluded.updated_at
            """,
            (site, start_url, last_page_url, _now()),
        )

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        # 把 WAL 併回主檔：CI 只會提交 images.db，殘留在 -wal 的資料等於遺失
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError as exc:  # 有其他連線時可能失敗
            import logging

            logging.getLogger(__name__).warning("WAL checkpoint 失敗：%s", exc)
        self.conn.close()

    # ---------- 查詢 ----------

    def search(
        self,
        tags: Sequence[str] = (),
        match_any: bool = False,
        query: str | None = None,
        raw_query: bool = False,
        site: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        sql = "SELECT i.* FROM images i"

        if tags:
            placeholders = ",".join("?" * len(tags))
            sql += (
                " JOIN image_tags it ON it.image_id = i.id"
                " JOIN tags t ON t.id = it.tag_id"
            )
            where.append(f"t.name IN ({placeholders})")
            params.extend(tags)

        if query:
            # trigram 分詞查不到少於 3 個字元的詞，這種短查詢直接走 LIKE
            use_fts = self.has_fts and (raw_query or len(query.strip()) >= 3)
            if use_fts:
                sql += " JOIN images_fts f ON f.rowid = i.id"
                where.append("images_fts MATCH ?")
                # 非 raw 模式把整串包成 phrase，避免 - " * 等字元被當成 FTS 語法
                params.append(query if raw_query else '"' + query.replace('"', '""') + '"')
            else:
                where.append("(i.name LIKE ? OR i.description LIKE ?)")
                params.extend([f"%{query}%"] * 2)

        if site:
            where.append("i.site = ?")
            params.append(site)
        if since:
            where.append("i.published_at >= ?")
            params.append(since)
        if until:
            where.append("i.published_at <= ?")
            params.append(until)

        if where:
            sql += " WHERE " + " AND ".join(where)

        if tags and not match_any:
            # AND 語意：命中的相異標籤數必須等於指定數量
            sql += " GROUP BY i.id HAVING COUNT(DISTINCT t.name) = ?"
            params.append(len(tags))
        elif tags:
            sql += " GROUP BY i.id"

        sql += " ORDER BY i.published_at DESC, i.id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self.conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["tags"] = self.tags_of(item["id"])
            results.append(item)
        return results

    def tags_of(self, image_id: int) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT t.name FROM tags t
            JOIN image_tags it ON it.tag_id = t.id
            WHERE it.image_id = ?
            ORDER BY t.name
            """,
            (image_id,),
        ).fetchall()
        return [r["name"] for r in rows]

    def pages_by_status(self, status: str, site: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT url, error, updated_at FROM crawl_state WHERE status = ?"
        params: list[Any] = [status]
        if site:
            sql += " AND site = ?"
            params.append(site)
        sql += " ORDER BY updated_at DESC"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def stats(self) -> dict[str, Any]:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "images": one("SELECT COUNT(*) FROM images"),
            "tags": one("SELECT COUNT(*) FROM tags"),
            "with_size": one("SELECT COUNT(*) FROM images WHERE width IS NOT NULL"),
            "pages_done": one("SELECT COUNT(*) FROM crawl_state WHERE status = 'done'"),
            "pages_empty": one("SELECT COUNT(*) FROM crawl_state WHERE status = 'empty'"),
            "pages_error": one("SELECT COUNT(*) FROM crawl_state WHERE status = 'error'"),
            "fts": self.has_fts,
        }

    def top_tags(self, limit: int = 30) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            """
            SELECT t.name, COUNT(*) AS n FROM tags t
            JOIN image_tags it ON it.tag_id = t.id
            GROUP BY t.id ORDER BY n DESC, t.name LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [(r["name"], r["n"]) for r in rows]
