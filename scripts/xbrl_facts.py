#!/usr/bin/env python3
"""TDnet / EDINET の XBRL zip（Inline XBRL）から数値ファクトを抽出する。

PDF のテキスト抽出より誤りが少ないため、XBRL がある場合はこちらを優先する。
ラベルは iXBRL 表の行見出し（日本語）から取る。

使い方:
  python3 -I xbrl_facts.py <xbrl.zip> [--statement pl|bs|cf|ci|ss|summary|all] [--format table|json]
  python3 -I xbrl_facts.py <xbrl.zip> --list-files

出力:
  long : 1ファクト1行の TSV（サマリーはこちらが読みやすい）
  table: 行ごとに「表示順 | ラベル | 要素名 | コンテキスト別の値」を TSV で出す
  json : すべてのファクト（値は元の表示単位と円換算の両方）
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import zipfile
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

# 添付ファイル名に含まれる記号 → 財務諸表の種類
STATEMENT_CODES = {
    "bs": "貸借対照表 / 財政状態計算書",
    "pl": "損益計算書",
    "ci": "包括利益計算書",
    "ss": "株主資本等変動計算書",
    "cf": "キャッシュ・フロー計算書",
    "segment": "セグメント情報",
}
SCALE_UNIT = {0: "円", 3: "千円", 6: "百万円", 9: "十億円"}


def statement_of(fname: str) -> str:
    base = Path(fname).name
    if "/Summary/" in fname or base.startswith("tse-") and "-ixbrl" in base and "Attachment" not in fname:
        return "summary"
    m = re.search(r"-(?:ac|sc|an|sn|ed|qc|qn|ic|in)?(bs|pl|ci|ss|cf|pc|fs|sg)\d*-", base)
    if m:
        code = m.group(1)
        # fs = 財政状態計算書（IFRS の BS）、sg = セグメント情報、pc = 損益計算書（個別様式）
        return {"pc": "pl", "fs": "bs", "sg": "segment"}.get(code, code)
    # EDINET: 0105010_honbun_... は本文。財務諸表は jpcrp030000-asr... の中
    return "other"


class IXParser(HTMLParser):
    """ix:nonFraction と表の行見出しを拾う。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.facts = []
        self.row = None          # 現在の行のセルテキスト
        self.cell = None
        self.fact = None         # 解析中の ix:nonFraction
        self.fact_depth = 0
        self.row_facts = []
        self.order = 0
        self.ctx_blocks = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self.row, self.row_facts = [], []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "ix:nonfraction":
            self.fact = {"name": a.get("name"), "context": a.get("contextref"), "unit": a.get("unitref"),
                         "decimals": a.get("decimals"), "scale": int(a.get("scale") or 0),
                         "sign": a.get("sign"), "format": a.get("format"), "nil": a.get("xsi:nil") == "true",
                         "text": ""}
            self.fact_depth = 1
        elif self.fact is not None and tag == "ix:nonfraction":
            self.fact_depth += 1

    def handle_endtag(self, tag):
        if tag == "ix:nonfraction" and self.fact is not None:
            f = self.fact
            self.fact = None
            self.order += 1
            f["order"] = self.order
            f["cells_before"] = [c for c in (self.row or []) if c]
            if self.cell is not None:
                f["cells_before"].append("".join(self.cell).strip())
            self.facts.append(f)
            if self.row is not None:
                self.row_facts.append(f)
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            label = next((c for c in self.row if c and not re.fullmatch(r"[\d,.\-△▲()（）\s※*]+", c)), "")
            for f in self.row_facts:
                f["label"] = label
            self.row, self.row_facts = None, []

    def handle_data(self, data):
        if self.fact is not None:
            self.fact["text"] += data
        if self.cell is not None:
            self.cell.append(data)


CTX_RE = re.compile(r"<xbrli:context\b[^>]*id=\"([^\"]+)\"[^>]*>(.*?)</xbrli:context>", re.S)


def parse_contexts(doc: str) -> dict:
    out = {}
    for cid, body in CTX_RE.findall(doc):
        start = re.search(r"<xbrli:startDate>([^<]+)", body)
        end = re.search(r"<xbrli:endDate>([^<]+)", body)
        inst = re.search(r"<xbrli:instant>([^<]+)", body)
        members = re.findall(r"<xbrldi:explicitMember[^>]*dimension=\"([^\"]+)\"[^>]*>([^<]+)<", body)
        out[cid] = {
            "start": start.group(1) if start else None,
            "end": end.group(1) if end else None,
            "instant": inst.group(1) if inst else None,
            "members": {d: m.strip() for d, m in members},
        }
    return out


def to_value(f: dict) -> Decimal | None:
    if f["nil"]:
        return None
    t = f["text"].strip().replace(",", "").replace("△", "").replace("▲", "")
    t = t.replace("(", "").replace(")", "")
    if t in ("", "-", "―", "—"):
        return None
    try:
        v = Decimal(t)
    except Exception:
        return None
    if f.get("sign") == "-":
        v = -v
    return v


def extract(zip_path: str, statement: str = "all") -> dict:
    zf = zipfile.ZipFile(zip_path)
    files = [n for n in zf.namelist() if n.endswith(".htm") and ("ixbrl" in n or "honbun" in n)]
    contexts, facts = {}, []
    for n in sorted(files):
        st = statement_of(n)
        doc = zf.read(n).decode("utf-8", "replace")
        contexts.update(parse_contexts(doc))
        if statement != "all" and st != statement:
            continue
        p = IXParser()
        p.feed(doc)
        for f in p.facts:
            v = to_value(f)
            f["file"] = n
            f["statement"] = st
            f["value"] = str(v) if v is not None else None
            f["display_unit"] = SCALE_UNIT.get(f["scale"], f"10^{f['scale']}円") if f["unit"] == "JPY" else f["unit"]
            f["value_yen"] = str(v * (Decimal(10) ** f["scale"])) if (v is not None and f["unit"] == "JPY") else None
            f.setdefault("label", "")
            if not f["label"]:
                f["label"] = next((c for c in reversed(f["cells_before"]) if c and not re.fullmatch(r"[\d,.\-△▲()（）\s]+", c)), "")
            f.pop("cells_before", None)
            f.pop("text", None)
            facts.append(f)
    for f in facts:
        f["period"] = contexts.get(f["context"], {})
    return {"zip": zip_path, "files": sorted(files), "contexts": contexts, "facts": facts}


def as_table(res: dict) -> str:
    """要素名×ラベルを行、コンテキストを列にした TSV。"""
    rows, cols = {}, []
    for f in res["facts"]:
        key = (f["statement"], f["name"], f["label"])
        c = f["context"]
        if c not in cols:
            cols.append(c)
        rows.setdefault(key, {"order": f["order"], "unit": None, "vals": {}})
        if f["value"] is not None and rows[key]["unit"] is None:
            rows[key]["unit"] = f["display_unit"]  # nil ファクトは scale を持たないので単位判定に使わない
        rows[key]["vals"][c] = f["value"]
    for r in rows.values():
        r["unit"] = r["unit"] or ""
    out = []
    ctx = res["contexts"]

    def cdesc(c):
        p = ctx.get(c, {})
        per = p.get("instant") or f"{p.get('start')}~{p.get('end')}"
        mem = ",".join(v.split(":")[-1] for v in p.get("members", {}).values())
        return f"{c}[{per}{(' ' + mem) if mem else ''}]"

    for st in dict.fromkeys(k[0] for k in rows):
        keys = [k for k in rows if k[0] == st]
        used = [c for c in cols if any(c in rows[k]["vals"] for k in keys)]
        out.append(f"# statement={st}")
        out.append("\t".join(["order", "label", "name", "unit"] + [cdesc(c) for c in used]))
        for k in sorted(keys, key=lambda k: rows[k]["order"]):
            r = rows[k]
            out.append("\t".join([str(r["order"]), k[2], k[1], r["unit"]] + [r["vals"].get(c) or "" for c in used]))
        out.append("")
    return "\n".join(out)


def as_long(res: dict) -> str:
    """1ファクト1行の TSV（label, name, 期間, メンバー, 値, 単位, statement）。nil は除く。"""
    out = ["statement\torder\tlabel\tname\tperiod\tmembers\tvalue\tunit\tcontext"]
    for f in res["facts"]:
        if f["value"] is None:
            continue
        p = f["period"]
        per = p.get("instant") or f"{p.get('start')}~{p.get('end')}"
        mem = ",".join(v.split(":")[-1] for v in p.get("members", {}).values())
        out.append("\t".join([f["statement"], str(f["order"]), f["label"], f["name"], per, mem,
                              f["value"], f["display_unit"] or "", f["context"]]))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zip")
    ap.add_argument("--statement", default="all", choices=["all", "summary", *STATEMENT_CODES.keys(), "other"],
                    help="bs=貸借対照表/財政状態計算書, pl=損益計算書, ci=包括利益計算書（IFRS の1計算書方式は PL を含む）")
    ap.add_argument("--format", default="table", choices=["table", "long", "json"])
    ap.add_argument("--list-files", action="store_true")
    a = ap.parse_args()
    if a.list_files:
        zf = zipfile.ZipFile(a.zip)
        print(json.dumps([{"file": n, "statement": statement_of(n)} for n in zf.namelist() if n.endswith(".htm")],
                         ensure_ascii=False, indent=2))
        return
    res = extract(a.zip, a.statement)
    if a.format == "json":
        print(json.dumps(res, ensure_ascii=False, indent=1))
    elif a.format == "long":
        print(as_long(res))
    else:
        print(as_table(res))


if __name__ == "__main__":
    main()
