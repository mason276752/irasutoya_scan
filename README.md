# StockLibraryScan — 通用 HTML 圖片 metadata 爬蟲

傳統 HTML 爬蟲：純 HTTP + CSS selector，不跑瀏覽器、不做 JS 渲染、不打 JSON API。
抓到的圖片 metadata 存進 SQLite，標籤可搜尋。

換一個網站只要寫一份 YAML 設定檔，程式碼不用改。

## 安裝

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 使用

```bash
# 爬取（先用 --dry-run 確認 selector 對不對，不會寫入資料庫）
python -m scraper crawl -c config/irasutoya.yaml -d data/images.db --dry-run --limit 10

# 正式爬，跑滿 5 小時就收工（進度會保存，下次接著跑）
python -m scraper crawl -c config/irasutoya.yaml -d data/images.db \
    --max-runtime 18000 --log-file data/crawl.log

# 查詢
python -m scraper search -d data/images.db -t 地図              # 單一標籤
python -m scraper search -d data/images.db -t 地図 -t リクエスト  # 同時具備兩個標籤
python -m scraper search -d data/images.db -t 地図 -t 節分 --any # 任一標籤
python -m scraper search -d data/images.db -q 赤鬼              # 名稱／說明全文搜尋
python -m scraper search -d data/images.db --since 2012-01-01 --json

# 其他
python -m scraper stats  -d data/images.db      # 統計
python -m scraper tags   -d data/images.db      # 標籤排行
python -m scraper errors -d data/images.db      # 列出失敗頁面（--status empty 看無圖片的頁）
python -m scraper retry  -c config/irasutoya.yaml -d data/images.db   # 重跑失敗頁面
python -m scraper export -d data/images.db --format csv --out out.csv
```

## 速度控制

單執行緒，一次一個請求，不做並發。設定在 YAML 的 `politeness`：

| 參數 | 說明 |
|---|---|
| `delay` | 任兩次請求之間至少間隔幾秒 |
| `jitter` | 再加 0~jitter 秒隨機抖動 |
| `retries` / `backoff` | 失敗重試次數與指數退避；429/503 會尊重 `Retry-After` |
| `respect_robots` | 是否遵守 robots.txt（預設 true） |

命令列可用 `--delay` / `--jitter` 臨時覆寫。**圖片尺寸量測也走同一套限速**。

## 設定檔

完整註解見 [config/example.yaml](config/example.yaml)。重點：

- `listing.item_link` — 列表頁上指向詳細頁的連結
- `listing.pagination.next_page` — 「下一頁」連結；或改用 `url_template` 套頁碼
- `detail.item` — 一頁有多張圖時，先框出每張圖的區塊
- `detail.fields` — 各欄位的 CSS selector

欄位規則可以寫成**一串**，依序嘗試、第一條有值的就採用，用來對付同站不同版型：

```yaml
name:
  - { selector: "img", attr: alt }
  - { selector: "h2", attr: text, scope: page }   # 沒有 alt 時退回標題
```

兩個特別的用法：

- `scope: page` — 從整份文件抽，而不是目前的圖片區塊。一頁多圖但說明／日期／標籤共用時要用它。
- `selector: "."` — 指區塊自己（例如 `item` 是 `<a>`，要抽它自己的 `href`）。

## 資料庫

標籤走正規化三表，所以標籤天然無序、不重複，多標籤查詢走索引：

```
images(id, site, page_url, image_url, name, description,
       width, height, published_at, fetched_at)   UNIQUE(page_url, image_url)
tags(id, name UNIQUE)
image_tags(image_id, tag_id)                      + 反向索引 (tag_id, image_id)
crawl_state(url, status, error, ...)              done / empty / error
crawl_cursor(site, start_url, last_page_url)      翻頁進度
```

`name` / `description` 另有 FTS5 全文索引（優先用 trigram 分詞，對中日文子字串搜尋友善）。

直接用 SQL 查也可以：

```sql
-- 同時具備「地図」和「リクエスト」的圖
SELECT i.* FROM images i
JOIN image_tags it ON it.image_id = i.id
JOIN tags t ON t.id = it.tag_id
WHERE t.name IN ('地図', 'リクエスト')
GROUP BY i.id HAVING COUNT(DISTINCT t.name) = 2;
```

### 已知限制

- FTS5 的 trigram 分詞查不到**少於 3 個字元**的詞。`search -q` 遇到短查詢會自動改用 `LIKE`，
  但 `--raw`（直接寫 FTS5 語法）模式下短詞仍然查不到。
- 重複執行時，同一張圖以 `(page_url, image_url)` 為鍵更新；標籤集合以最新一次抓取為準。

## 續爬與增量

- 詳細頁處理完會記進 `crawl_state`，重跑時跳過（`done` 和 `empty` 都跳過，`error` 會重試）。
- 列表翻頁進度記在 `crawl_cursor`，一輪跑不完時下次從中斷的列表頁接著跑。
- 走到最新一頁後，游標停在那裡；之後每次執行都從最新頁開始，只處理新增的內容。
- `--restart` 可忽略游標，從 `start_urls` 重新來過。

## GitHub Actions

[.github/workflows/crawl.yml](.github/workflows/crawl.yml) 每週日 02:00（台北）執行一次，也可手動觸發：

- 單次跑 5 小時（`--max-runtime 18000`）就收工，避開 GitHub 6 小時的 job 上限
- **已有 run 在執行時，新的一次直接跳過**（不排隊、不重複執行）
- 跑完接著重試先前失敗的頁面
- 完整 log 上傳成 artifact（保留 30 天）
- `data/images.db` 提交回 repo，下次執行接續
- **手動取消時整批丟棄**：不重試、不上傳 log、不提交資料庫，該次進度不保留。
  爬取本身失敗（非取消）則照常保住已抓到的資料

> 資料庫是二進位檔，每次提交都會讓 repo 長大。如果累積後太大，改成用
> `actions/cache` 或 Release assets 保存會比較好。

## 測試

`tests/` 底下有一個自製的假站台，可以在不打真站的情況下驗證整條流程：

```bash
python tests/make_fixtures.py
cd tests/fixtures/site && python -m http.server 8765 &
python -m scraper crawl -c tests/fixture_site.yaml -d /tmp/test.db
```
