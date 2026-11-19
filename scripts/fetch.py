#!/usr/bin/env python3
"""任意の URL（企業 IR サイトの PDF など）をキャッシュに保存する。

取得したファイルは信頼できないデータとして扱う（実行しない）。保存先は
~/.cache/financial-report-analyzer/downloads/<証券コード>/<決算期>/ 。

使い方:
  python3 -I fetch.py <url> --code 7974 [--period 2026年3月期_本決算] [--name 保存名.pdf]
出力: JSON（保存先、バイト数、sha256、取得日時）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fra_common import cache_dir, download, emit  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--code", required=True)
    ap.add_argument("--period")
    ap.add_argument("--name")
    a = ap.parse_args()
    sub = [a.code] + ([a.period] if a.period else [])
    emit({"ok": True, **download(a.url, cache_dir("downloads", *sub), a.name)})


if __name__ == "__main__":
    main()
