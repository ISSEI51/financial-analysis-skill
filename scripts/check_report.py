#!/usr/bin/env python3
"""完成した決算ノートの整合性検査。

1. fra:table ブロックが、データ JSON から再生成した表と一致するか
2. 図（SVG）の埋め込みが存在し、ファイルがあるか。CF 未開示なら CF 図がないことを確認
   ビジネスモデル：data/bizmodel/<決算期>.json の検証、図（BM・BM-<事業>）の埋め込み、主体の表（fra:table:bizmodel）の照合。
   図の生成に失敗した記録（<決算期>_BM_figures.json の ok=false）は警告にとどめる
3. 図の数値（<prefix>_figures.json）とデータ JSON の値が一致するか
4. 本文中の金額（兆円・億円・百万円・千円）と比率（%）が、データ・計算指標のどれかと一致するか
   一致しないものは警告として列挙する（出典つきで data.extra に登録するか、本文を直す）
5. 必須の章立て・frontmatter・【事実】【解釈】【仮説】の使用・出典一覧

使い方:
  python3 -I check_report.py --note <決算ノート.md> --data <data.json> [--vault <vault>]
終了コード: 0=エラーなし（警告はあり得る）, 1=エラーあり
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fin import TABLES, growth, load, metrics  # noqa: E402
from fra_common import D, UNIT_TO_YEN, emit, round_half_up  # noqa: E402
from obsidian import AUTO_E, AUTO_S, fm_parse_simple, split_note  # noqa: E402

REQUIRED_HEADINGS = ["3分でわかる決算概要", "ビジネスモデル", "損益計算書", "貸借対照表", "キャッシュ・フロー",
                     "7つの観点", "今回の決算からわかったこと", "まだ判断できないこと", "次回決算で確認すべき指標",
                     "参考資料・出典"]
REQUIRED_FM = ["会社名", "証券コード", "業種", "決算期", "対象期間", "資料公表日", "分析日", "会計基準", "連結・単体", "tags"]
AMOUNT_RE = re.compile(r"([△▲\-−+＋]?)(?:(\d[\d,]*)兆)?(\d[\d,]*(?:\.\d+)?)?\s*(兆円|億円|百万円|千円)")
PCT_RE = re.compile(r"([△▲\-−+＋]?)(\d+(?:\.\d+)?)\s*(%|％|ポイント|pt)")


def known_amounts(d: dict) -> list[Decimal]:
    """データ中の金額（円換算）。"""
    unit = d["basis"]["unit"]
    vals = []

    def walk(o, u):
        if isinstance(o, dict):
            u2 = o.get("unit", u) if isinstance(o.get("unit"), str) and o.get("unit") in UNIT_TO_YEN else u
            for k, v in o.items():
                if k in ("value", "prior", "revenue", "revenue_prior", "profit", "profit_prior", "operating_income",
                         "ordinary_income", "net_income_parent", "assets", "segment_assets", "capex", "amount") or k.endswith("_amount"):
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        vals.append(D(v) * UNIT_TO_YEN[u2])
                if k == "values" and isinstance(v, dict):
                    for x in v.values():
                        if isinstance(x, (int, float, str)) and D(x) is not None:
                            try:
                                vals.append(D(x) * UNIT_TO_YEN[u2])
                            except Exception:
                                pass
                walk(v, u2)
        elif isinstance(o, list):
            for x in o:
                walk(x, u)
    walk({k: v for k, v in d.items() if k not in ("sources",)}, unit)
    # 差額（増減）と計算指標の金額
    for sec in ("pl", "cf"):
        for ln in (d.get(sec) or {}).get("lines", []):
            v, p = D(ln.get("value")), D(ln.get("prior"))
            if v is not None and p is not None:
                vals.append((v - p) * UNIT_TO_YEN[unit])
    bs = d.get("bs") or {}
    for side in ("assets", "liabilities", "equity"):
        for ln in bs.get(side, []):
            v, p = D(ln.get("value")), D(ln.get("prior"))
            if v is not None and p is not None:
                vals.append((v - p) * UNIT_TO_YEN[unit])
    for t in list((bs.get("totals") or {}).values()) + list((bs.get("group_totals") or {}).values()) + [bs.get("owners_equity") or {}]:
        if isinstance(t, dict) and D(t.get("value")) is not None and D(t.get("prior")) is not None:
            vals.append((D(t["value"]) - D(t["prior"])) * UNIT_TO_YEN[unit])
    for m in metrics(d)["metrics"]:
        if m["unit"] in UNIT_TO_YEN:
            for f in ("value", "prior"):
                if m[f] is not None:
                    vals.append(D(m[f]) * UNIT_TO_YEN[m["unit"]])
            if m["value"] is not None and m["prior"] is not None:
                vals.append((D(m["value"]) - D(m["prior"])) * UNIT_TO_YEN[m["unit"]])
    for s in d.get("segments") or []:
        for a, b in (("revenue", "revenue_prior"), ("profit", "profit_prior")):
            if D(s.get(a)) is not None and D(s.get(b)) is not None:
                vals.append((D(s[a]) - D(s[b])) * UNIT_TO_YEN[unit])
    fc = d.get("forecast") or {}
    return vals


def known_pcts(d: dict) -> list[Decimal]:
    out = []
    for m in metrics(d)["metrics"]:
        if m["unit"] == "%":
            for f in ("value", "prior"):
                if m[f] is not None:
                    out.append(D(m[f]))
            if m["value"] is not None and m["prior"] is not None:
                out.append(D(m["value"]) - D(m["prior"]))   # ポイント差
    pl = d.get("pl") or {}
    rev = rp = None
    for ln in pl.get("lines", []):
        if ln.get("key") == pl.get("revenue_key", "revenue"):
            rev, rp = D(ln.get("value")), D(ln.get("prior"))
    for sec in ("pl", "cf"):
        for ln in (d.get(sec) or {}).get("lines", []):
            g, _ = growth(ln.get("value"), ln.get("prior"))
            if g is not None:
                out.append(g)
            v, p = D(ln.get("value")), D(ln.get("prior"))
            if v is not None and rev:
                out.append(v / rev * 100)
            if p is not None and rp:
                out.append(p / rp * 100)
            if v is not None and rev and p is not None and rp:
                out.append(v / rev * 100 - p / rp * 100)
    bs = d.get("bs") or {}
    ta = D(((bs.get("totals") or {}).get("assets") or {}).get("value"))
    tap = D(((bs.get("totals") or {}).get("assets") or {}).get("prior"))
    for side in ("assets", "liabilities", "equity"):
        for ln in bs.get(side, []):
            v, p = D(ln.get("value")), D(ln.get("prior"))
            if v is not None and ta:
                out.append(v / ta * 100)
            if p is not None and tap:
                out.append(p / tap * 100)
            g, _ = growth(v, p)
            if g is not None:
                out.append(g)
    for t in list((bs.get("group_totals") or {}).values()) + list((bs.get("totals") or {}).values()):
        if isinstance(t, dict) and D(t.get("value")) is not None and ta:
            out.append(D(t["value"]) / ta * 100)
            g, _ = growth(t.get("value"), t.get("prior"))
            if g is not None:
                out.append(g)
    segs = d.get("segments") or []
    tot = sum((D(s.get("revenue")) or 0) for s in segs)
    totp = sum((D(s.get("revenue_prior")) or 0) for s in segs)
    for s in segs:
        r, rpv, pf, pp = D(s.get("revenue")), D(s.get("revenue_prior")), D(s.get("profit")), D(s.get("profit_prior"))
        rt, rtp = D(s.get("revenue_total")), D(s.get("revenue_total_prior"))
        if pf is not None and rt:
            out.append(pf / rt * 100)
        if pp is not None and rtp:
            out.append(pp / rtp * 100)
        for g in (growth(r, rpv)[0], growth(pf, pp)[0]):
            if g is not None:
                out.append(g)
        if r and tot:
            out.append(r / tot * 100)
        if rpv and totp:
            out.append(rpv / totp * 100)
        if pf is not None and r:
            out.append(pf / r * 100)
        if pp is not None and rpv:
            out.append(pp / rpv * 100)
    fc = d.get("forecast") or {}
    for k, v in fc.items():
        if k.endswith("_change_pct") and v is not None:
            out.append(D(v))
    for e in d.get("extra") or []:
        if e.get("unit") in ("%", "％", "ポイント"):
            vals = e.get("values") if isinstance(e.get("values"), dict) else {"v": e.get("value")}
            out.extend(D(x) for x in vals.values() if D(x) is not None)
    return [x for x in out if x is not None]


ALIASES = {
    "revenue": ["売上高", "売上収益", "営業収益", "売上"],
    "gross_profit": ["売上総利益", "粗利益", "粗利"],
    "cogs": ["売上原価", "原価"],
    "sga": ["販管費", "販売費及び一般管理費"],
    "non_op_income": ["営業外収益"],
    "non_op_expense": ["営業外費用"],
    "income_taxes": ["法人税等", "税金費用", "法人所得税"],
    "receivables": ["売上債権", "売掛金"],
    "payables": ["仕入債務", "買掛金"],
    "ppe": ["有形固定資産"],
    "goodwill": ["のれん"],
    "operating_income": ["営業利益", "営業損失", "営業赤字"],
    "ordinary_income": ["経常利益", "経常損失"],
    "pretax_income": ["税引前利益", "税引前当期純利益", "税金等調整前当期純利益", "税引前中間純利益", "税金等調整前中間純利益",
                      "税金等調整前四半期純利益"],
    "net_income_parent": ["純利益", "純損失", "当期純利益", "中間純利益", "四半期純利益", "最終利益", "最終損益",
                          "親会社の所有者に帰属する当期利益", "親会社の所有者に帰属する中間利益",
                          "親会社株主に帰属する当期純利益", "親会社株主に帰属する中間純利益",
                          "親会社株主に帰属する四半期純利益"],
    "cfo": ["営業CF", "営業キャッシュ・フロー", "営業キャッシュフロー"],
    "cfi": ["投資CF", "投資キャッシュ・フロー", "投資キャッシュフロー"],
    "cff": ["財務CF", "財務キャッシュ・フロー", "財務キャッシュフロー"],
    "fcf": ["フリーキャッシュフロー", "フリーキャッシュ・フロー", "FCF"],
    "assets": ["総資産", "資産合計"],
    "net_assets": ["純資産"],
    "owners_equity": ["自己資本"],
    "liabilities": ["負債合計", "負債"],
    "cash": ["現金及び預金", "現預金", "手元資金"],
    "inventories": ["棚卸資産", "在庫"],
    "interest_bearing_debt": ["有利子負債", "借入金"],
}


def labelled_values(d: dict) -> dict:
    """ラベル → {"amt": [円換算の値], "pct": [%]}。本文の数値を直前の科目名で照合するために使う。"""
    unit = UNIT_TO_YEN[d["basis"]["unit"]]
    out: dict[str, dict] = {}

    def add(names, amts=(), pcts=(), u=unit):
        for n in names:
            if not n:
                continue
            e = out.setdefault(n, {"amt": [], "pct": []})
            e["amt"] += [D(a) * u for a in amts if D(a) is not None]
            e["pct"] += [D(p) for p in pcts if D(p) is not None]

    def names_for(ln):
        lab = ln.get("label", "")
        plain = re.sub(r"（[^）]*）|\([^)]*\)", "", lab)          # 「（△は増加）」などの注記を外す
        base = [lab, re.sub(r"合計$", "", lab), plain, re.sub(r"合計$", "", plain)]
        if "又は" in plain:   # 日本基準の「営業利益又は営業損失（△）」→「営業利益」「営業損失」
            base += plain.split("又は")
        return base + ALIASES.get(ln.get("key"), []) + list(ln.get("aliases") or [])

    pl = d.get("pl") or {}
    rev = next((ln for ln in pl.get("lines", []) if ln.get("key") == pl.get("revenue_key", "revenue")), {})
    R, RP = D(rev.get("value")), D(rev.get("prior"))
    fc = d.get("forecast") or {}
    for sec in ("pl", "cf"):
        for ln in (d.get(sec) or {}).get("lines", []):
            v, p = D(ln.get("value")), D(ln.get("prior"))
            amts = [v, p] + ([v - p] if (v is not None and p is not None) else [])
            pcts = [growth(v, p)[0]]
            if sec == "pl" and R:
                pcts += [v / R * 100 if v is not None else None, p / RP * 100 if (p is not None and RP) else None]
                if v is not None and p is not None and RP:
                    pcts.append(v / R * 100 - p / RP * 100)
            key = ln.get("key")
            fkey = {"revenue": "revenue"}.get(key, key)
            if fkey in fc and fc.get(fkey) is not None:
                amts.append(fc[fkey])
                pcts.append(fc.get(f"{fkey}_change_pct"))
                if v is not None and D(fc[fkey]):
                    pcts.append(v / D(fc[fkey]) * 100)
                prev = (fc.get("previous") or {}).get(fkey)   # 修正前の会社予想と修正幅
                if prev is not None and D(prev):
                    amts += [prev, D(fc[fkey]) - D(prev)]
                    pcts.append(growth(fc[fkey], prev)[0])
            add(names_for(ln), amts, pcts)
    bs = d.get("bs") or {}
    ta = D(((bs.get("totals") or {}).get("assets") or {}).get("value"))
    tap = D(((bs.get("totals") or {}).get("assets") or {}).get("prior"))
    rows = [ln for side in ("assets", "liabilities", "equity") for ln in bs.get(side, [])]
    rows += [{"label": k, **v} for k, v in (bs.get("group_totals") or {}).items()]
    rows += [{"key": k, "label": v.get("label", ""), **v} for k, v in (bs.get("totals") or {}).items() if isinstance(v, dict)]
    if bs.get("owners_equity"):
        rows.append({"key": "owners_equity", **bs["owners_equity"]})
    for ln in rows:
        v, p = D(ln.get("value")), D(ln.get("prior"))
        amts = [v, p] + ([v - p] if (v is not None and p is not None) else [])
        pcts = [growth(v, p)[0], v / ta * 100 if (v is not None and ta) else None, p / tap * 100 if (p is not None and tap) else None]
        add(names_for(ln), amts, pcts)
    for m in metrics(d)["metrics"]:
        names = [m["label"], re.sub(r"（.*?）", "", m["label"])] + ALIASES.get(m["key"], [])
        v, p = D(m["value"]), D(m["prior"])
        vals = [v, p] + ([v - p] if (v is not None and p is not None) else [])
        if m["unit"] == "%":
            add(names, pcts=vals)
        elif m["unit"] in UNIT_TO_YEN:
            add(names, amts=vals, u=UNIT_TO_YEN[m["unit"]])
    segs = d.get("segments") or []
    tot = sum((D(x.get("revenue")) or 0) for x in segs)
    for sg in segs:
        r, rp, pf, pp = (D(sg.get(k)) for k in ("revenue", "revenue_prior", "profit", "profit_prior"))
        rt, rtp = D(sg.get("revenue_total")) or r, D(sg.get("revenue_total_prior")) or rp
        amts = [r, rp, pf, pp, rt, rtp] + [a - b for a, b in ((r, rp), (pf, pp)) if a is not None and b is not None]
        pcts = [growth(r, rp)[0], growth(pf, pp)[0], pf / rt * 100 if (pf is not None and rt) else None,
                pp / rtp * 100 if (pp is not None and rtp) else None, r / tot * 100 if (r and tot) else None]
        add([sg["name"]], amts, pcts)
    # 売上内訳（単一セグメントの会社）。財務諸表と別の単位で開示されていれば revenue_breakdown_unit で換算する
    rb = d.get("revenue_breakdown") or []
    rbu = UNIT_TO_YEN.get(d.get("revenue_breakdown_unit") or d["basis"]["unit"], unit)
    rtot = sum((D(x.get("value")) or 0) for x in rb)
    for x in rb:
        v, p = D(x.get("value")), D(x.get("prior"))
        amts = [v, p] + ([v - p] if (v is not None and p is not None) else [])
        pcts = [growth(v, p)[0], v / rtot * 100 if (v is not None and rtot) else None]
        add([x["label"]], amts, pcts, u=rbu)
    for e in d.get("extra") or []:
        vals = list(e["values"].values()) if isinstance(e.get("values"), dict) else [e.get("value")]
        lab = e["label"]
        names = [lab, re.sub(r"（.*?）", "", lab)] + list(e.get("aliases") or [])
        first = re.split(r"\s", lab)[0]          # 「デジタル売上高 前年同期比」→「デジタル売上高」
        head = re.split(r"の|（|\s", lab)[0]      # 「ゲーム専用機の売上高」→「ゲーム専用機」
        for h in (first, head):
            # 自動の別名は日本語を含む3文字以上の語だけ（「Nintendo」のような英字だけの語は広すぎる）
            if len(h) >= 3 and h != lab and re.search(r"[^\x00-\x7f]", h):
                names.append(h)
        if e.get("unit") in UNIT_TO_YEN:
            add(names, amts=vals, u=UNIT_TO_YEN[e["unit"]])
        elif e.get("unit") in ("%", "％", "ポイント"):
            add(names, pcts=vals)
    return out


def nearest_label(sentence_before: str, labels: list[str]) -> str | None:
    """数値の直前に最も近い科目名。同じ位置で終わる候補は最も長いもの（「棚卸資産」と「資産」なら前者）。"""
    best, end = None, -1
    for lab in labels:
        i = sentence_before.rfind(lab)
        if i == -1:
            continue
        e = i + len(lab)
        if e > end or (e == end and len(lab) > len(best)):
            best, end = lab, e
    return best


def labels_in(sentence: str, labels: list[str]) -> list[str]:
    return [lab for lab in labels if lab in sentence]


def match_amount(yen: Decimal, places: int, unit_yen: Decimal, known: list[Decimal]) -> bool:
    shown = yen / unit_yen
    for k in known:
        kv = k / unit_yen
        for cand in (round_half_up(kv, places), kv.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)):
            if abs(abs(cand) - abs(shown)) <= Decimal(1).scaleb(-places) / 2:
                return True
    return False


def match_pct(v: Decimal, places: int, known: list[Decimal]) -> bool:
    for k in known:
        if abs(abs(round_half_up(k, places)) - abs(v)) <= Decimal(1).scaleb(-places) / 2:
            return True
    return False


def strip_tables(text: str) -> str:
    """表（スクリプト生成）と、照合から外す範囲（<!-- fra:nocheck --> … <!-- /fra:nocheck -->）を除く。"""
    text = re.sub(r"<!-- fra:table:(\w+) -->.*?<!-- /fra:table:\1 -->", "", text, flags=re.S)
    return re.sub(r"<!-- fra:nocheck -->.*?<!-- /fra:nocheck -->", "", text, flags=re.S)


def check_bizmodel(note: Path, data: Path, d: dict, period: str, auto: str, found: dict, embeds: list,
                   errs: list, warns: list) -> None:
    """ビジネスモデル JSON（data/bizmodel/<決算期>.json）の検証、図の生成記録と埋め込み、主体の表の照合。
    図の生成に失敗した記録がある場合は警告にとどめる（財務分析は続行できる。原因は利用者に報告する）。"""
    import hashlib
    from bizmodel import load_json as bm_load, table as bm_table, validate as bm_validate
    bm_path = data.parent / "bizmodel" / f"{period}.json"
    if not bm_path.exists():
        errs.append(f"ビジネスモデル JSON（data/bizmodel/{period}.json）がない（bizmodel.py init で作成し、主体と取引関係を資料から記入する）")
        return
    bm = bm_load(bm_path)
    bv = bm_validate(bm, d)
    errs += [f"ビジネスモデル JSON：{x}" for x in bv["errors"]]
    if bm.get("draft"):
        errs.append("ビジネスモデル JSON が draft=true のまま（レビュー未完了）")
    for s in bm.get("sources") or []:
        if s.get("url") and s["url"] not in auto:
            errs.append(f"ビジネスモデルの出典 {s.get('id')} の URL が本文の出典一覧にない: {s['url']}")
    if bv["ok"]:
        if "bizmodel" not in found:
            errs.append("表 fra:table:bizmodel が本文にない（bizmodel.py table の出力を「ビジネスモデル」の章に貼る）")
        elif found["bizmodel"].strip() != bm_table(bm).strip():
            errs.append("表 fra:table:bizmodel がビジネスモデル JSON から再生成した内容と一致しない（手で書き換えていないか）")
    rec = note.parent / "assets" / f"{period}_BM_figures.json"
    if not rec.exists():
        errs.append("ビジネスモデル図がない（diagrams.py build または bizmodel.py build を実行する）")
        return
    r = json.loads(rec.read_text(encoding="utf-8"))
    if r.get("bizmodel_sha256") != hashlib.sha256(bm_path.read_bytes()).hexdigest():
        errs.append("ビジネスモデル図が JSON の更新前に作られている（diagrams.py build を再実行する）")
    if not r.get("ok"):
        warns.append(f"ビジネスモデル図の生成に失敗した（段階：{r.get('stage')}、原因：{r.get('error')}）。"
                     "財務分析は続行できるが、原因を利用者に報告すること")
        return
    m = re.search(r"^#{2,3}\s+[^\n]*ビジネスモデル[^\n]*$", auto, re.M)
    sec = ""
    if m:
        nxt = re.search(r"^##\s", auto[m.end():], re.M)
        sec = auto[m.end(): m.end() + nxt.start()] if nxt else auto[m.end():]
    for f in r.get("figures", []):
        name = f"{period}_{f['figure']}.svg"
        if not any(e.endswith(name) for e in embeds):
            errs.append(f"ビジネスモデル図（{name}）が埋め込まれていない（obsidian.py embeds --kind BM）")
        elif name not in sec:
            warns.append(f"ビジネスモデル図（{name}）が「ビジネスモデル」の章の外にある")


def check(note: Path, data: Path, vault: Path | None) -> dict:
    errs, warns = [], []
    d = load(data)
    text = note.read_text(encoding="utf-8")
    fm_lines, rest = split_note(text)
    fm = fm_parse_simple(fm_lines)
    auto = rest[rest.find(AUTO_S) + len(AUTO_S): rest.find(AUTO_E)] if AUTO_S in rest else rest
    # ---- frontmatter
    for k in REQUIRED_FM:
        if k not in fm or fm[k] in ("", None, []):
            errs.append(f"frontmatter に {k} がない")
    if str(fm.get("証券コード", "")).strip()[:4] != str(d["company"]["code"]).strip()[:4]:
        errs.append("frontmatter の証券コードがデータと異なる")
    if d.get("draft"):
        errs.append("データ JSON が draft=true のまま（レビュー未完了）")
    # ---- 章立て
    heads = re.findall(r"^#{1,4}\s+(.+)$", auto, re.M)
    for h in REQUIRED_HEADINGS:
        if not any(h in x for x in heads):
            errs.append(f"必須の章「{h}」がない")
    for tag in ("【事実】", "【解釈】", "【仮説】"):
        if tag not in auto:
            warns.append(f"{tag} の記載がない（分析の3階層を区別すること）")
    if "判断材料不足" not in auto and "まだ判断できない" not in auto:
        warns.append("情報不足の扱い（判断材料不足）の記載がない")
    for s in d.get("sources", []):
        if s.get("url") and s["url"] not in auto:
            errs.append(f"出典 {s.get('id')} の URL が本文の出典一覧にない: {s['url']}")
    # ---- 表
    found = {m.group(2): m.group(1) for m in re.finditer(r"(<!-- fra:table:(\w+) -->.*?<!-- /fra:table:\2 -->)", auto, re.S)}
    for k in ("pl", "bs", "cf", "metrics"):
        if k not in found:
            errs.append(f"表 fra:table:{k} が本文にない（fin.py tables の出力を貼ること）")
    for k, blk in found.items():
        if k in TABLES:
            if (k == "bs" and not d.get("bs")):
                continue
            exp = TABLES[k](d).strip()
            if blk.strip() != exp:
                errs.append(f"表 fra:table:{k} がデータ JSON から再生成した内容と一致しない（手で数値を書き換えていないか）")
    # ---- 図
    embeds = re.findall(r"!\[\[([^\]|]+\.svg)(?:\|[^\]]*)?\]\]", auto)
    period = d["period"]["label"]
    want = ["PL"] + (["BS"] if d.get("bs") else []) + ([] if (d.get("cf") or {}).get("disclosed") is False else ["CF"])
    for kind in want:
        if not any(e.endswith(f"{period}_{kind}.svg") for e in embeds):
            errs.append(f"{kind} 図（{period}_{kind}.svg）が埋め込まれていない")
    if (d.get("cf") or {}).get("disclosed") is False and any(e.endswith("_CF.svg") for e in embeds):
        errs.append("CF 未開示なのに CF 図が埋め込まれている")
    if vault:
        for e in embeds:
            if not (vault / e).exists():
                errs.append(f"埋め込み先のファイルがない: {e}")
            dio = (vault / e).with_suffix(".drawio")
            if not dio.exists():
                errs.append(f"編集用 .drawio がない: {dio}")
    figs = note.parent / "assets" / f"{period}_figures.json"
    if figs.exists():
        fj = json.loads(figs.read_text(encoding="utf-8"))
        import hashlib
        if fj.get("data_sha256") != hashlib.sha256(data.read_bytes()).hexdigest():
            errs.append("図がデータ JSON の更新前に作られている（diagrams.py build を再実行すること）")
        for f in fj.get("figures", []):
            if not f["verify"]["ok"]:
                errs.append(f"{f['figure']} 図の検証エラー: {f['verify']['errors'][:3]}")
    else:
        warns.append(f"{figs.name} がない（diagrams.py build の結果を保存すること）")
    # ---- ビジネスモデル（JSON・図・表）
    check_bizmodel(note, data, d, period, auto, found, embeds, errs, warns)
    # ---- 本文の数値
    body = strip_tables(auto)
    body = re.sub(r"!\[\[[^\]]+\]\]", "", body)
    body = re.sub(r"\[[^\]]*\]\([^)]*\)", "", body)
    ka, kp = known_amounts(d), known_pcts(d)
    lv = labelled_values(d)
    labels_sorted = sorted(lv, key=len, reverse=True)
    unmatched = []

    BOUND = "。\n|"

    def sentence_at(pos):
        st = max(body.rfind(c, 0, pos) for c in BOUND)
        ens = [body.find(c, pos) for c in BOUND]
        en = min([e for e in ens if e != -1] or [len(body)])
        return body[st + 1:pos], body[st + 1:en]

    def judge(m, kind, value, places, unit_yen=None):
        before, sent = sentence_at(m.start())
        cands = labels_in(sent, labels_sorted)
        ctx = body[max(0, m.start() - 30): m.end() + 10].replace("\n", " ")
        if cands:
            for lab in cands:
                vals = lv[lab]["amt" if kind == "amt" else "pct"]
                if vals and (match_amount(value, places, unit_yen, vals) if kind == "amt" else match_pct(value, places, vals)):
                    return
            # 同じ文に出てくる科目のどれにも一致しない
            if any(lv[lab]["amt" if kind == "amt" else "pct"] for lab in cands):
                unmatched.append({"text": m.group(0), "context": ctx, "label": nearest_label(before, cands) or cands[0]})
                return
        ok = match_amount(value, places, unit_yen, ka) if kind == "amt" else match_pct(value, places, kp)
        if not ok:
            unmatched.append({"text": m.group(0), "context": ctx, "label": None})

    for m in AMOUNT_RE.finditer(body):
        sign, cho, num, unit = m.groups()
        if not cho and not num:
            continue
        yen = Decimal(0)
        places = 0
        if cho:
            yen += Decimal(cho.replace(",", "")) * UNIT_TO_YEN["兆円"]
        if num:
            n = num.replace(",", "")
            places = len(n.split(".")[1]) if "." in n else 0
            yen += Decimal(n) * UNIT_TO_YEN["億円" if cho else unit]
        judge(m, "amt", yen, places, UNIT_TO_YEN["億円" if cho else unit])
    for m in PCT_RE.finditer(body):
        sign, num, unit = m.groups()
        places = len(num.split(".")[1]) if "." in num else 0
        judge(m, "pct", Decimal(num), places)
    for u in unmatched:
        if u["label"]:
            warns.append(f"本文の数値「{u['text']}」が同じ文の科目（直前は「{u['label']}」）のデータ・計算値と一致しない：…{u['context']}…")
        else:
            warns.append(f"本文の数値「{u['text']}」がデータ・計算値のどれとも一致しない：…{u['context']}…")
    from fra_common import VERSION
    return {"ok": not errs, "version": VERSION, "note": str(note), "errors": errs, "warnings": warns, "n_embeds": len(embeds),
            "n_tables": len(found), "n_unmatched_numbers": len(unmatched)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--note", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--vault")
    a = ap.parse_args()
    r = check(Path(a.note), Path(a.data), Path(a.vault) if a.vault else None)
    emit(r)
    sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main()
