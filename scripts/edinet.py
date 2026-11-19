#!/usr/bin/env python3
"""EDINET API v2 で有価証券報告書・半期報告書を検索・取得する。

API キー（Subscription-Key）が必要。https://api.edinet-fsa.go.jp/ で無料登録し、
環境変数 EDINET_API_KEY（設定 edinet_api_key_env で変更可）に入れる。キーは設定ファイルに書かない。

書類種別コード（docTypeCode）: 120=有価証券報告書, 130=訂正有価証券報告書, 160=半期報告書,
                              140=四半期報告書（2024年4月以降は廃止）, 350=大量保有報告書 など

使い方:
  python3 -I edinet.py search --code 7974 --from 2026-06-01 --to 2026-06-30 [--types 120,160]
  python3 -I edinet.py download --doc-id S100XXXX --code 7974 [--type pdf|xbrl|csv]
出力: JSON。日次の書類一覧はキャッシュする（当日分を除く）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fra_common import cache_dir, die, emit, http_get, load_config  # noqa: E402

API = "https://api.edinet-fsa.go.jp/api/v2"
TYPE_PARAM = {"xbrl": 1, "pdf": 2, "csv": 5}


def api_key() -> str:
    env = load_config().get("edinet_api_key_env", "EDINET_API_KEY")
    k = os.environ.get(env)
    if not k:
        die(f"EDINET の API キーが未設定（環境変数 {env}）。企業 IR サイトの PDF を使うか、利用者にキー登録を依頼すること", code=3)
    return k


def day_list(d: date, key: str) -> list[dict]:
    cache = cache_dir("edinet", "lists") / f"{d.isoformat()}.json"
    if cache.exists() and d < date.today():
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        raw = http_get(f"{API}/documents.json?date={d.isoformat()}&type=2&Subscription-Key={key}", binary=False)
    except urllib.error.HTTPError as e:
        die(f"EDINET API エラー HTTP {e.code}（キーが無効の可能性）")
    j = json.loads(raw)
    if j.get("metadata", {}).get("status") not in ("200", 200):
        die(f"EDINET API エラー: {j.get('metadata') or j}")
    res = j.get("results") or []
    if d < date.today():
        cache.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return res


def cmd_search(a):
    key = api_key()
    d0, d1 = date.fromisoformat(a.frm), date.fromisoformat(a.to)
    types = set(a.types.split(",")) if a.types else None
    hits, d = [], d0
    while d <= d1:
        for r in day_list(d, key):
            sc = (r.get("secCode") or "")
            if not sc.upper().startswith(a.code.upper()):
                continue
            if types and r.get("docTypeCode") not in types:
                continue
            hits.append({k: r.get(k) for k in ("docID", "secCode", "filerName", "docTypeCode", "docDescription",
                                               "periodStart", "periodEnd", "submitDateTime", "pdfFlag", "xbrlFlag",
                                               "withdrawalStatus", "parentDocID")})
        d += timedelta(days=1)
    emit({"ok": True, "count": len(hits), "results": hits,
          "note": "parentDocID がある書類は訂正報告書。withdrawalStatus!=0 は取下げ"})


def cmd_download(a):
    key = api_key()
    t = TYPE_PARAM[a.type]
    data = http_get(f"{API}/documents/{a.doc_id}?type={t}&Subscription-Key={key}")
    if data[:1] == b"{":
        die(f"EDINET API エラー: {data[:300].decode('utf-8', 'replace')}")
    ext = {"xbrl": "zip", "pdf": "pdf", "csv": "zip"}[a.type]
    dest = cache_dir("downloads", a.code, "edinet") / f"{a.doc_id}_{a.type}.{ext}"
    dest.write_bytes(data)
    import hashlib
    emit({"ok": True, "path": str(dest), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
          "url": f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{a.doc_id}"})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("search")
    s.add_argument("--code", required=True)
    s.add_argument("--from", dest="frm", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--types")
    s.set_defaults(func=cmd_search)
    d = sp.add_parser("download")
    d.add_argument("--doc-id", required=True)
    d.add_argument("--code", required=True)
    d.add_argument("--type", default="pdf", choices=list(TYPE_PARAM))
    d.set_defaults(func=cmd_download)
    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
