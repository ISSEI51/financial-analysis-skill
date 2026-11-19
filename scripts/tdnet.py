#!/usr/bin/env python3
"""TDnet（適時開示情報閲覧サービス）の検索・取得。

TDnet が公開している期間は直近 約1か月 に限られる。それより古い資料は
企業 IR サイトまたは EDINET（edinet.py）から取得すること。

使い方:
  python3 -I tdnet.py search --code 7203 [--from 2026-10-18 --to 2026-11-18] [--title 決算短信]
  python3 -I tdnet.py search --q トヨタ
  python3 -I tdnet.py download --url https://www.release.tdnet.info/inbs/xxxx.pdf --code 7203 [--period 2027年3月期_第2四半期]

出力は JSON（stdout）。
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fra_common import cache_dir, die, download, emit, load_config  # noqa: E402

BASE = "https://www.release.tdnet.info"
SEARCH_URL = BASE + "/onsf/TDJFSearch/TDJFSearch"

ROW_RE = re.compile(r"<tr class=\"(?:odd|even)\">(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<td class=\"(time|code|companyname|title|xbrl|exchange|update)\"[^>]*>(.*?)</td>", re.S)
HREF_RE = re.compile(r"href=\"([^\"]+)\"")
TAG_RE = re.compile(r"<[^>]+>")

# 決算短信の表題から期・種別を読み取る
KIND_PATTERNS = [
    ("決算短信", re.compile(r"決算短信")),
    ("決算説明資料", re.compile(r"説明|プレゼンテーション|presentation|補足|ハイライト|概要", re.I)),
    ("業績予想修正", re.compile(r"業績予想|修正")),
    ("配当", re.compile(r"配当")),
    ("訂正", re.compile(r"訂正")),
]
QTR_RE = re.compile(r"第\s*([１２３1-3])\s*四半期")


def _text(s: str) -> str:
    return html.unescape(TAG_RE.sub("", s)).strip()


def classify(title: str) -> dict:
    import unicodedata
    title = unicodedata.normalize("NFKC", title)
    kinds = [k for k, rx in KIND_PATTERNS if rx.search(title)]
    q = QTR_RE.search(title)
    qn = None
    if q:
        qn = int(q.group(1).translate(str.maketrans("１２３", "123")))
    standard = None
    for key, std in (("IFRS", "IFRS"), ("米国基準", "US-GAAP"), ("日本基準", "J-GAAP")):
        if key in title:
            standard = std
    scope = "単体" if "非連結" in title else ("連結" if "(連結)" in title else None)
    return {
        "kinds": kinds,
        "quarter": qn,
        "is_annual": ("決算短信" in title and qn is None and "中間" not in title),
        "is_interim": "中間" in title,
        "standard": standard,
        "scope": scope,
        "is_correction": "訂正" in title,
    }


def search(q: str, d_from: date, d_to: date) -> list[dict]:
    body = urllib.parse.urlencode({"t0": d_from.strftime("%Y%m%d"), "t1": d_to.strftime("%Y%m%d"),
                                   "q": q, "m": "0"}).encode()
    req = urllib.request.Request(SEARCH_URL, data=body, headers={"User-Agent": load_config()["user_agent"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        page = r.read().decode("utf-8", "replace")
    rows = []
    for m in ROW_RE.finditer(page):
        cells = {k: v for k, v in CELL_RE.findall(m.group(1))}
        if "title" not in cells:
            continue
        title = _text(cells["title"])
        pdf = HREF_RE.search(cells["title"])
        xbrl = HREF_RE.search(cells.get("xbrl", ""))
        rows.append({
            "datetime": _text(cells.get("time", "")),
            "code5": _text(cells.get("code", "")),
            "company": _text(cells.get("companyname", "")),
            "title": title,
            "pdf_url": urllib.parse.urljoin(BASE + "/inbs/", pdf.group(1)) if pdf else None,
            "xbrl_url": urllib.parse.urljoin(BASE + "/inbs/", xbrl.group(1)) if xbrl else None,
            "exchange": _text(cells.get("exchange", "")),
            **classify(title),
        })
    return rows


def cmd_search(a):
    d_to = date.fromisoformat(a.to) if a.to else date.today()
    d_from = date.fromisoformat(a.frm) if a.frm else d_to - timedelta(days=31)
    if (d_to - d_from).days > 31:
        # TDnet の検索は 1 か月が上限。区間を分割して検索する。
        pass
    q = a.code or a.q
    if not q:
        die("--code か --q を指定してください")
    results, cur = [], d_from
    while cur <= d_to:
        end = min(cur + timedelta(days=30), d_to)
        results.extend(search(q, cur, end))
        cur = end + timedelta(days=1)
    if a.code:
        code = a.code.upper()
        results = [r for r in results if r["code5"].upper().startswith(code) and len(r["code5"]) == 5]
    if a.title:
        rx = re.compile(a.title)
        results = [r for r in results if rx.search(r["title"])]
    results.sort(key=lambda r: r["datetime"], reverse=True)
    emit({"ok": True, "query": q, "from": str(d_from), "to": str(d_to), "count": len(results),
          "note": "TDnet の公開期間は直近約1か月。古い資料は IR サイト / EDINET を使う。",
          "results": results})


def cmd_download(a):
    sub = [a.code or "unknown"]
    if a.period:
        sub.append(a.period)
    dest = cache_dir("downloads", *sub)
    meta = download(a.url, dest)
    if meta["path"].endswith(".zip"):
        meta["note"] = "XBRL zip。xbrl_facts.py で数値を抽出する。"
    emit({"ok": True, **meta})


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("search")
    s.add_argument("--code")
    s.add_argument("--q")
    s.add_argument("--from", dest="frm")
    s.add_argument("--to")
    s.add_argument("--title", help="表題の正規表現フィルタ（例: 決算短信|説明資料）")
    s.set_defaults(func=cmd_search)
    d = sp.add_parser("download")
    d.add_argument("--url", required=True)
    d.add_argument("--code")
    d.add_argument("--period")
    d.set_defaults(func=cmd_download)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
