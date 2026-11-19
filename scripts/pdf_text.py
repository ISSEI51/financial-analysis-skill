#!/usr/bin/env python3
"""PDF をページ番号つきテキストに変換する（poppler の pdftotext を使用）。

出典に頁番号を書くため、各ページの先頭に「=== p.N ===」を入れる。
暗号化（印刷・コピー許可つき）の決算短信 PDF もそのまま読める。

使い方:
  python3 -I pdf_text.py <file.pdf> [--out <file.txt>] [--pages 3-10] [--grep "キャッシュ・フロー|セグメント"] [--raw]
  --grep: 一致した行を「p.頁:行」の形で出す（本文を読む位置を探すのに使う）
  --raw : レイアウト保持をやめる（段組みの説明資料で読みやすい場合がある）
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path


def page_count(pdf: str) -> int:
    out = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True).stdout
    m = re.search(r"Pages:\s+(\d+)", out)
    return int(m.group(1)) if m else 0


def extract(pdf: str, first: int, last: int, raw: bool) -> list[str]:
    pages = []
    for p in range(first, last + 1):
        cmd = ["pdftotext", "-f", str(p), "-l", str(p), "-enc", "UTF-8"]
        if not raw:
            cmd.append("-layout")
        cmd += [pdf, "-"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        pages.append(r.stdout)
    return pages


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--out")
    ap.add_argument("--pages")
    ap.add_argument("--grep")
    ap.add_argument("--raw", action="store_true")
    a = ap.parse_args()
    if not shutil.which("pdftotext"):
        sys.exit("pdftotext が見つからない。macOS は `brew install poppler` で導入する")
    n = page_count(a.pdf)
    first, last = 1, n
    if a.pages:
        x = a.pages.split("-")
        first, last = int(x[0]), int(x[-1])
    pages = extract(a.pdf, first, min(last, n), a.raw)
    if a.grep:
        rx = re.compile(a.grep)
        for i, t in enumerate(pages, start=first):
            for ln in t.splitlines():
                if rx.search(ln):
                    print(f"p.{i}: {ln.strip()}")
        return
    text = "".join(f"\n=== p.{i} ===\n{t}" for i, t in enumerate(pages, start=first))
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f'{{"ok": true, "pages": {n}, "out": "{a.out}", "chars": {len(text)}}}')
    else:
        print(text)


if __name__ == "__main__":
    main()
