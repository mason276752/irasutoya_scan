"""產生本地假站台（HTML + 圖片），給端對端測試用。

跑法：python tests/make_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).parent / "fixtures" / "site"

LISTING = """<!doctype html>
<html><head><meta charset="utf-8"><title>列表 {page}</title></head>
<body>
  <div class="post-list">
    {items}
  </div>
  {next_link}
</body></html>
"""

ITEM_LINK = '<div class="post"><a class="thumb" href="{href}"><img src="{thumb}"></a></div>'

DETAIL = """<!doctype html>
<html><head><meta charset="utf-8">
  <meta property="og:title" content="{title}（og）">
</head>
<body>
  <h1 class="title">{title}</h1>
  <div class="entry">
    <p>{desc}</p>
    {imgs}
  </div>
  <time datetime="{date}">{date}</time>
  <div class="tags">{tags}</div>
</body></html>
"""

# 備援版型：沒有 h1／沒有 div.entry img／日期是日文寫法／標籤擠在一行
DETAIL_ALT = """<!doctype html>
<html><head><meta charset="utf-8">
  <meta property="og:title" content="{title}">
  <meta name="description" content="{desc}">
  <meta property="og:image" content="{img}">
</head>
<body>
  <span class="date">{date}</span>
  <div class="tags"><a rel="tag" href="#">{tags}</a></div>
</body></html>
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_image(path: Path, size: tuple[int, int], color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def main() -> None:
    make_image(ROOT / "img/a.png", (300, 200), "#e34")
    make_image(ROOT / "img/b.jpg", (640, 480), "#48c")
    make_image(ROOT / "img/c.gif", (128, 128), "#2a6")
    make_image(ROOT / "img/d.png", (1024, 768), "#fa0")
    make_image(ROOT / "img/e.jpg", (800, 600), "#909")

    items = [
        ("item1.html", "貓咪貼圖", "一隻在睡覺的貓。", "2026-07-01", ["動物", "貓"], ["img/a.png"]),
        ("item2.html", "上班族插圖", "拿著公事包的上班族。", "2026-07-15", ["人物", "工作"], ["img/b.jpg"]),
        ("item3.html", "雙圖頁面", "同一頁有兩張圖。", "2026-07-20", ["動物", "鳥"],
         ["img/c.gif", "img/d.png"]),
    ]

    for href, title, desc, date, tags, imgs in items:
        write(
            ROOT / href,
            DETAIL.format(
                title=title,
                desc=desc,
                date=date,
                imgs="\n    ".join(f'<img src="{i}" alt="{title}">' for i in imgs),
                tags="".join(f'<a rel="tag" href="#">{t}</a>' for t in tags),
            ),
        )

    write(
        ROOT / "item4.html",
        DETAIL_ALT.format(
            title="備援版型的圖",
            desc="這頁沒有 h1，欄位得靠備援規則抽。",
            img="img/e.jpg",  # 相對路徑，靠 type: url 補成絕對網址
            date="2026年7月30日",
            tags="風景、天空／夏天",
        ),
    )

    page1 = LISTING.format(
        page=1,
        items="\n    ".join(
            ITEM_LINK.format(href=h, thumb=imgs[0]) for h, _, _, _, _, imgs in items
        ),
        next_link='<a class="next-page" href="page2.html">下一頁</a>',
    )
    write(ROOT / "index.html", page1)

    page2 = LISTING.format(
        page=2,
        items=ITEM_LINK.format(href="item4.html", thumb="img/e.jpg"),
        next_link="",  # 最後一頁：沒有下一頁連結
    )
    write(ROOT / "page2.html", page2)

    write(ROOT / "robots.txt", "User-agent: *\nDisallow: /private/\n")
    print(f"fixtures 已產生於 {ROOT}")


if __name__ == "__main__":
    main()
