#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""損害保険ニュースまとめ 自動生成スクリプト

Google News RSS から損保関連ニュースを収集し、Anthropic API (Claude) で
記事の選定・要約・「代理店実務への影響」コメントを生成して index.html を更新する。
ANTHROPIC_API_KEY が未設定の場合はルールベースの簡易生成にフォールバックする。

GitHub Actions (.github/workflows/daily-news.yml) から毎朝実行される。
ローカルでの手動実行も可:  python scripts/collect_news.py
"""

import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))
WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]

# 収集対象の検索クエリ(Google News RSS)
QUERIES = [
    "損害保険",
    "保険代理店",
    "金融庁 保険",
    "自動車保険 保険料",
    "火災保険",
    "地震保険",
    "東京海上 OR 損保ジャパン OR 三井住友海上 OR あいおいニッセイ",
    "大雨 OR 台風 OR 地震 警戒",
]

MAX_AGE_HOURS = 48        # 収集対象: 直近48時間
MAX_CANDIDATES = 40       # Claude に渡す候補の上限
MIN_CANDIDATES = 3        # これ未満なら更新せず異常終了(前日のページを保持)

CATEGORIES = ["制度", "災害", "料率", "各社", "代理店", "市場"]
CAT_CLASS = {"制度": "", "災害": "saigai", "料率": "shijo", "市場": "shijo",
             "各社": "kakusha", "代理店": "dairiten"}
CAT_ICON = {"制度": "🏛️", "災害": "🌧️", "料率": "📊", "市場": "📈",
            "各社": "🏢", "代理店": "🔍"}

# ---------------------------------------------------------------- 選定ルール

DISASTER_WORDS = ["大雨", "台風", "地震", "豪雨", "洪水", "土砂", "噴火", "津波",
                  "警報", "線状降水帯", "猛暑", "大雪"]
MAJOR_INSURERS = ["東京海上", "損保ジャパン", "三井住友海上", "あいおい", "SOMPO",
                  "MS&AD", "ソニー損保", "チューリッヒ", "アクサ", "楽天損保",
                  "セコム", "日新火災", "共栄火災"]

# 無条件に除外する語 (投資信託の基準価額ページ・マーケットデータなど)
EXCLUDE_WORDS = ["基準価額", "基準価格", "投資信託", "投信", "ファンド", "ETF",
                 "NISA", "iDeCo"]

# 生命保険のみの話題を示す語 (下の SONPO_WORDS が併記されていれば除外しない)
LIFE_WORDS = ["生命保険", "生保", "医療保険", "がん保険", "終身保険", "養老保険",
              "個人年金", "死亡保険", "学資保険", "日本生命", "第一生命",
              "住友生命", "明治安田", "かんぽ生命", "アフラック", "メットライフ"]

# 損保実務に関係することを示す語
SONPO_WORDS = (["損害保険", "損保", "自動車保険", "火災保険", "地震保険",
                "傷害保険", "賠償", "金融庁", "保険代理店", "代理店"]
               + MAJOR_INSURERS)


def is_excluded(title: str) -> bool:
    """収集段階で除外すべき記事か (投信の基準価額ページ・生保のみの話題)。"""
    if any(w in title for w in EXCLUDE_WORDS):
        return True
    if any(w in title for w in LIFE_WORDS) and not any(w in title for w in SONPO_WORDS):
        return True
    return False


# フォールバック選定の優先度 (高いほど優先。複数該当は加点)
PRIORITY_RULES: list[tuple[int, list[str]]] = [
    (4, ["自動車保険", "火災保険", "地震保険"]),        # 主要損保商品
    (4, DISASTER_WORDS),                                # 災害情報
    (3, ["金融庁", "監督指針", "保険業法", "規制", "法改正"]),  # 監督動向
    (3, MAJOR_INSURERS),                                # 大手各社の動向
    (2, ["損害保険", "損保", "保険代理店", "代理店", "保険料", "料率"]),
]


def priority_score(title: str) -> int:
    return sum(pt for pt, words in PRIORITY_RULES
               if any(w in title for w in words))


def fetch_rss(query: str) -> list[dict]:
    """Google News RSS を取得して記事リストを返す。"""
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query)
           + "&hl=ja&gl=JP&ceid=JP:ja")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (sonpo-news bot)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        tree = ET.parse(resp)
    items = []
    for item in tree.getroot().iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        source = (item.findtext("source") or "").strip()
        if not title or not link:
            continue
        try:
            dt = parsedate_to_datetime(pub).astimezone(JST)
        except Exception:
            continue
        items.append({"title": title, "link": link, "source": source, "published": dt})
    return items


def collect_candidates(now: datetime) -> list[dict]:
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)
    seen_titles: list[str] = []
    candidates: list[dict] = []
    for q in QUERIES:
        try:
            items = fetch_rss(q)
        except Exception as e:
            print(f"WARN: RSS取得失敗 query={q!r}: {e}", file=sys.stderr)
            continue
        for it in items:
            if it["published"] < cutoff:
                continue
            # 投信の基準価額ページ・生保のみの話題などを除外
            if is_excluded(it["title"]):
                continue
            # タイトルの重複(ほぼ同一記事)を除外
            norm = re.sub(r"\s+", "", it["title"]).split(" - ")[0][:25]
            if any(norm in s or s in norm for s in seen_titles):
                continue
            seen_titles.append(norm)
            candidates.append(it)
    candidates.sort(key=lambda x: x["published"], reverse=True)
    return candidates[:MAX_CANDIDATES]


# ---------------------------------------------------------------- Claude 選定

ARTICLE_SCHEMA = {
    "type": "object",
    "properties": {
        "articles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer",
                              "description": "候補リストの番号 (0始まり)"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "icon": {"type": "string",
                             "description": "見出し用の絵文字1文字"},
                    "headline": {"type": "string",
                                 "description": "読者向けに整えた見出し(30〜40字程度)"},
                    "summary": {"type": "string",
                                "description": "記事要約(100〜130字程度、である調)"},
                    "impact": {"type": "string",
                               "description": "代理店実務への影響・アクション(40〜60字程度)"},
                    "source_label": {"type": "string",
                                     "description": "出典表記 例: 日本経済新聞 2026/7/4"},
                },
                "required": ["index", "category", "icon", "headline",
                             "summary", "impact", "source_label"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["articles"],
    "additionalProperties": False,
}


def select_with_claude(candidates: list[dict], now: datetime) -> list[dict] | None:
    """Claude で記事選定・要約。失敗したら None を返しフォールバックさせる。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("INFO: ANTHROPIC_API_KEY 未設定のためルールベース生成にフォールバック")
        return None
    try:
        import anthropic
    except ImportError:
        print("WARN: anthropic SDK 未インストールのためフォールバック", file=sys.stderr)
        return None

    listing = "\n".join(
        f"[{i}] {c['title']} | {c['source']} | {c['published'].strftime('%Y/%m/%d %H:%M')}"
        for i, c in enumerate(candidates)
    )
    prompt = f"""あなたは損害保険代理店向け日刊ニュースレター「損害保険ニュースまとめ」の編集者です。
本日は {now.strftime('%Y年%m月%d日')} です。以下はここ48時間のニュース候補一覧です。

{listing}

この中から、損害保険代理店の実務者にとって重要なニュースを6〜7本選び、各記事について
カテゴリ(制度/災害/料率/各社/代理店/市場)、絵文字アイコン、整えた見出し、
100〜130字程度の要約(である調)、代理店実務への影響(40〜60字、具体的なアクション)、
出典表記を作成してください。

優先して選ぶもの:
- 損害保険商品(自動車保険・火災保険・地震保険)の商品・保険料・支払いに関する動き
- 金融庁など当局の監督・規制・処分の動向
- 災害情報(大雨・台風・地震など契約者被害につながるもの)。あれば先頭に配置する
- 大手損保グループ各社(東京海上・SOMPO・MS&AD など)の経営・サービスの動向

除外するもの(選ばない):
- 投資信託の基準価額(基準価格)ページや、株価・マーケットデータだけの記事
- 生命保険のみに関する話題(損保・代理店実務にも関係する場合は選んでよい)
- 芸能・スポーツなど損保実務と無関係な話題

注意:
- 同一の話題は1本にまとめる
- 憶測は書かず、見出しから確実に読み取れる範囲で要約する(不明な詳細は書かない)"""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": ARTICLE_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            print("WARN: Claude が応答を拒否したためフォールバック", file=sys.stderr)
            return None
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
    except Exception as e:
        print(f"WARN: Claude 呼び出し失敗のためフォールバック: {e}", file=sys.stderr)
        return None

    articles = []
    for a in data.get("articles", []):
        i = a.get("index")
        if not isinstance(i, int) or not (0 <= i < len(candidates)):
            continue
        articles.append({
            "category": a["category"],
            "icon": a["icon"],
            "headline": a["headline"],
            "summary": a["summary"],
            "impact": a["impact"],
            "source_label": a["source_label"],
            "link": candidates[i]["link"],
            "title": candidates[i]["title"],
        })
    return articles or None


# ------------------------------------------------------------ フォールバック

def categorize(title: str) -> str:
    # 商品名「地震保険」の「地震」は災害情報ではないため判定から除く
    if any(w in title.replace("地震保険", "") for w in DISASTER_WORDS):
        return "災害"
    rules = [
        ("代理店", ["代理店"]),
        ("料率", ["料率", "保険料", "値上げ", "引き上げ", "改定"]),
        ("制度", ["金融庁", "保険業法", "法改正", "規制", "監督指針", "制度"]),
        ("各社", MAJOR_INSURERS),
    ]
    for cat, words in rules:
        if any(w in title for w in words):
            return cat
    return "市場"


def select_fallback(candidates: list[dict]) -> list[dict]:
    """AI なしの簡易選定: 優先度スコア順(同点は新しい順)に最大7本、災害を先頭へ。"""
    ranked = sorted(
        candidates,
        key=lambda c: (-priority_score(c["title"]), -c["published"].timestamp()),
    )
    picked = ranked[:7]
    articles = []
    for c in picked:
        cat = categorize(c["title"])
        # Google News のタイトル末尾 " - 媒体名" を除去
        headline = re.sub(r"\s*-\s*[^-]+$", "", c["title"])
        articles.append({
            "category": cat,
            "icon": CAT_ICON[cat],
            "headline": headline,
            "summary": "",
            "impact": "",
            "source_label": f"{c['source']} {c['published'].strftime('%Y/%m/%d')}",
            "link": c["link"],
            "title": c["title"],
        })
    articles.sort(key=lambda a: 0 if a["category"] == "災害" else 1)
    return articles


# ---------------------------------------------------------------- HTML 生成

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>損害保険ニュースまとめ</title>
<style>
:root{{--navy:#1a5276;--c-seido:#2471a3;--c-kakusha:#1e8449;--c-saigai:#c0392b;--c-shijo:#7d3c98;--c-dairiten:#b7770d}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Hiragino Kaku Gothic ProN","Yu Gothic",Meiryo,sans-serif;background:#f6f8fa;color:#2c3e50;line-height:1.6}}
.wrap{{max-width:800px;margin:0 auto;padding:20px 16px}}
header{{background:linear-gradient(135deg,#1a5276,#2980b9);color:#fff;border-radius:12px;padding:16px 22px;margin-bottom:14px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}}
header h1{{font-size:1.4em;letter-spacing:.04em}}
header .sub{{font-size:.85em;opacity:.9}}
.avatar{{width:56px;height:56px;border-radius:50%;object-fit:cover;object-position:50% 15%;border:2px solid #fff;display:none}}
article{{background:#fff;border-radius:10px;padding:12px 16px;margin-bottom:10px;box-shadow:0 1px 4px rgba(0,0,0,.08);border-left:6px solid var(--c-seido)}}
article.saigai{{border-left-color:var(--c-saigai)}}
article.kakusha{{border-left-color:var(--c-kakusha)}}
article.shijo{{border-left-color:var(--c-shijo)}}
article.dairiten{{border-left-color:var(--c-dairiten)}}
.cat{{display:inline-block;font-size:.72em;font-weight:bold;color:#fff;background:var(--c-seido);border-radius:20px;padding:2px 12px;margin-right:6px;vertical-align:middle}}
.saigai .cat{{background:var(--c-saigai)}}
.kakusha .cat{{background:var(--c-kakusha)}}
.shijo .cat{{background:var(--c-shijo)}}
.dairiten .cat{{background:var(--c-dairiten)}}
h2{{font-size:1em;display:inline;vertical-align:middle}}
.icon{{margin-right:4px}}
.src{{font-size:.75em;color:#7f8c8d;margin-left:4px}}
p.sum{{margin-top:5px;font-size:.88em}}
p.impact{{margin-top:5px;font-size:.85em;background:#fef9e7;border-radius:6px;padding:3px 10px}}
p.impact::before{{content:"💡 代理店実務への影響: ";font-weight:bold}}
footer{{margin-top:14px;border-top:2px solid var(--navy);padding-top:8px;font-size:.8em;color:#555}}
footer ul{{margin:4px 0 6px 1.2em}}
a{{color:#1a5ca8}}
.note{{font-size:.75em;color:#888}}
.backnumber{{margin-top:8px;font-size:.8em}}
@media print{{
 @page{{size:A4;margin:10mm}}
 body{{background:#fff;font-size:9pt}}
 .wrap{{max-width:none;padding:0}}
 header{{border-radius:6px;padding:8px 14px;margin-bottom:8px;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
 header h1{{font-size:1.15em}}
 article{{box-shadow:none;border:1px solid #ddd;border-left-width:5px;padding:5px 10px;margin-bottom:5px;page-break-inside:avoid;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
 p.sum{{font-size:8.5pt}}
 p.impact{{font-size:8pt}}
 a{{color:#222;text-decoration:none}}
 .backnumber{{display:none}}
 footer{{margin-top:8px}}
}}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div>
    <h1>📰 損害保険ニュースまとめ</h1>
    <div class="sub">{date_line}発行|毎朝6時更新・損保実務に効くニュースをA4一枚で</div>
  </div>
  <img class="avatar" src="img/editor.jpg" alt="編集人" onload="this.style.display='block'" onerror="this.remove()">
</header>

{articles_html}
<footer>
  <strong>出典一覧</strong>
  <ul>
{sources_html}
  </ul>
  <p class="note">本ページは公開情報の自動収集・要約です。詳細は各出典をご確認ください。</p>
  <div class="backnumber"><strong>バックナンバー</strong>
    <ul>
{backnumber_html}
    </ul>
  </div>
  <p style="text-align:center;font-size:.85em;margin-top:10px;color:#7f8c8d"><a href="mailto:s-tsuji@universal-life.co.jp" style="color:#7f8c8d;text-decoration:none">大阪ＡＩ支社</a></p>
</footer>

</div>
</body>
</html>
"""


def render_article(a: dict) -> str:
    cls = CAT_CLASS.get(a["category"], "")
    cls_attr = f' class="{cls}"' if cls else ""
    parts = [
        f"<article{cls_attr}>",
        f'  <span class="cat">{html.escape(a["category"])}</span>'
        f'<h2><span class="icon">{a["icon"]}</span>{html.escape(a["headline"])}</h2>'
        f'<span class="src">{html.escape(a["source_label"])}</span>',
    ]
    if a.get("summary"):
        parts.append(f'  <p class="sum">{html.escape(a["summary"])}</p>')
    if a.get("impact"):
        parts.append(f'  <p class="impact">{html.escape(a["impact"])}</p>')
    parts.append("</article>")
    return "\n".join(parts)


def render_page(articles: list[dict], now: datetime, backnumbers: list[str],
                is_archive: bool) -> str:
    date_line = (f"{now.year}年{now.month}月{now.day}日"
                 f"({WEEKDAYS_JA[now.weekday()]})")
    articles_html = "\n\n".join(render_article(a) for a in articles) + "\n"
    sources_html = "\n".join(
        f'    <li><a href="{html.escape(a["link"], quote=True)}">'
        f'{html.escape(a["headline"])}({html.escape(a["source_label"])})</a></li>'
        for a in articles
    )
    bn_lines = []
    for name in backnumbers:
        m = re.match(r"sonpo-news-(\d{4})(\d{2})(\d{2})\.html", name)
        if not m:
            continue
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        label = f"{y}年{mo}月{d}日号"
        href = name if is_archive else f"archive/{name}"
        bn_lines.append(f'      <li><a href="{href}">{label}</a></li>')
    backnumber_html = "\n".join(bn_lines)
    return PAGE_TEMPLATE.format(
        date_line=date_line,
        articles_html=articles_html,
        sources_html=sources_html,
        backnumber_html=backnumber_html,
    )


def main() -> int:
    now = datetime.now(JST)
    print(f"INFO: 実行開始 {now.isoformat()}")

    candidates = collect_candidates(now)
    print(f"INFO: 候補記事 {len(candidates)} 件")
    if len(candidates) < MIN_CANDIDATES:
        print("ERROR: 候補記事が不足。ページを更新せず終了します。", file=sys.stderr)
        return 1

    articles = select_with_claude(candidates, now)
    mode = "claude"
    if not articles:
        articles = select_fallback(candidates)
        mode = "fallback"
    print(f"INFO: 掲載記事 {len(articles)} 件 (mode={mode})")

    archive_dir = REPO_ROOT / "archive"
    archive_dir.mkdir(exist_ok=True)
    today_name = f"sonpo-news-{now.strftime('%Y%m%d')}.html"

    # バックナンバー一覧(本日号を含む・新しい順・最大14件)
    names = {p.name for p in archive_dir.glob("sonpo-news-*.html")}
    names.add(today_name)
    backnumbers = sorted(names, reverse=True)[:14]

    index_html = render_page(articles, now, backnumbers, is_archive=False)
    archive_html = render_page(articles, now, backnumbers, is_archive=True)
    (REPO_ROOT / "index.html").write_text(index_html, encoding="utf-8")
    (archive_dir / today_name).write_text(archive_html, encoding="utf-8")
    print(f"INFO: index.html / archive/{today_name} を更新しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
