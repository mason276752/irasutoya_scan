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

# 匯出 + 看圖頁
python -m scraper export -d data/images.db --format json --out data/images.json
python -m scraper viewer -d data/images.db --out data/index.html
cd data && python -m http.server 8080     # 開 http://localhost:8080
```

## 看圖頁

`viewer` 產生的 `index.html` 會讀同目錄的 `images.json`，提供縮圖牆、關鍵字搜尋、
標籤篩選（可多選，全部符合／任一符合切換）、依日期或尺寸排序，並支援深色模式。

因為瀏覽器的安全限制，用 `file://` 直接開啟時讀不到 `images.json`，這時頁面會提示你
起一個本機服務，或直接用頁面上的檔案選擇器手動載入 JSON。想要單一檔案就能開，
改用 `--embed` 把資料嵌進 HTML：

```bash
python -m scraper viewer -d data/images.db --out viewer.html --embed
```

## 速度控制

**網頁一律單執行緒序列**，一次一個請求；**圖片量測則可並行**（只讀檔頭、成本低）。
設定在 YAML 的 `politeness`：

| 參數 | 說明 |
|---|---|
| `delay` | 網頁請求之間至少間隔幾秒 |
| `jitter` | 再加 0~jitter 秒隨機抖動 |
| `image_concurrency` | 同時最多量測幾張圖（預設 10） |
| `image_delay` | 圖片請求之間的最小間隔（預設 0，只靠並行數限制） |
| `retries` / `backoff` | 失敗重試次數與 5xx 的指數退避 |
| `too_many_requests_wait` | 被 429 擋下時暫停多久（預設 60 秒） |
| `respect_robots` | 是否遵守 robots.txt（預設 true） |

**429 是全域冷卻**：任何一個請求收到 429，整條通道（含並行中的圖片執行緒）都會一起暫停，
不是只讓撞到的那一個等。伺服器有給 `Retry-After` 時取兩者較大值。

命令列可用 `--delay` / `--jitter` 臨時覆寫網頁的間隔。

量測批次是**以頁面為單位**收集的：一個詳細頁上的所有圖會一次打包並行量測。
（設定檔常把「每張圖」寫成一個 `detail.item`，若在 item 層級量測，每次只有一張圖，
並行等於沒開。）

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
- **同一個圖檔出現在多篇文章時，會存成多筆**（一頁一筆）。這是刻意的：唯一鍵含 `page_url`，
  所以「這張圖在這篇文章裡叫什麼」會被完整保留。例如 `paint_hoka1_01_kuten.png` 同時在
  平假名頁與片假名頁出現，兩筆的名稱分別是「ひらがなのペンキ文字『句点』」和
  「カタカナのペンキ文字『句点』」。查相異圖檔用 `SELECT DISTINCT image_url FROM images`。

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

## GitHub Pages

看圖頁會自動發布成網站，網址是 `https://<帳號>.github.io/<repo>/`。

**第一次要先在 repo 設定裡開啟**：Settings → Pages → Build and deployment →
Source 選 **GitHub Actions**（不是 Deploy from a branch）。沒設定的話部署那一步會失敗。

站台只包含看圖需要的三個檔案，資料庫和 log 不會上傳：

```
index.html      看圖頁
images.json     資料（頁面用 fetch 讀它）
images.csv      給你自己下載用
```

兩個 workflow 都會部署：

- [crawl.yml](.github/workflows/crawl.yml) — 每週爬完後自動部署最新資料
- [pages.yml](.github/workflows/pages.yml) — 改了 `scraper/viewer.html` 之後 push 就重新部署，
  不重爬；也可以手動觸發。它用的是 repo 裡現有的 `data/images.db`

> 資料量大時 `images.json` 會跟著變大（約每千張 600KB）。Pages 傳輸有 gzip，
> 實際下載量約為三分之一，但圖片數量上萬之後首次載入仍會有明顯等待。

## 測試

`tests/` 底下有一個自製的假站台，可以在不打真站的情況下驗證整條流程：

```bash
python tests/make_fixtures.py
cd tests/fixtures/site && python -m http.server 8765 &
python -m scraper crawl -c tests/fixture_site.yaml -d /tmp/test.db
```
