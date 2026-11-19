#!/usr/bin/env python3
"""決算データ JSON の検証・指標計算・Markdown 表の生成。

データ形式は references/data-schema.md を参照。

使い方:
  python3 -I fin.py validate <data.json>             # 整合性検証（exit 0=OK, 1=重大な不整合あり）
  python3 -I fin.py metrics  <data.json>             # 指標を計算して JSON で出力
  python3 -I fin.py tables   <data.json> [--only pl,bs,cf,metrics,segments,forecast]
  python3 -I fin.py quarter  <当期累計.json> <前四半期累計.json>   # 単独四半期の差分算出
  python3 -I fin.py history  <data_dir>               # 同一企業の複数期を時系列比較

すべての計算は Decimal で行う（浮動小数点の丸め誤差を避ける）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fra_common import D, emit, fmt_num, fmt_pct, round_half_up  # noqa: E402

SIGN = {"revenue": 1, "income": 1, "expense": -1, "attribution": -1}
CHAIN_KINDS = {"revenue", "income", "expense", "attribution", "subtotal"}
PERIOD_TYPES = {"FY": "本決算", "Q1": "第1四半期", "Q2": "第2四半期", "Q3": "第3四半期"}
STANDARDS = {"J-GAAP", "IFRS", "US-GAAP", "JMIS"}


# ======================================================================== 読み込み

def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def fv(ln: dict, field: str = "value"):
    """行の値。決算書で「－」（計上なし）と表示された欄（nil_fields）は 0、未開示は None。"""
    v = D(ln.get(field))
    if v is None and field in (ln.get("nil_fields") or []):
        return Decimal(0)
    return v


def cell(ln: dict, field: str = "value") -> str:
    if ln.get(field) is None and field in (ln.get("nil_fields") or []):
        return "－"
    return fmt_num(ln.get(field))


def lines_by_key(lines: list[dict]) -> dict:
    return {ln["key"]: ln for ln in lines if ln.get("key")}


def val(d: dict, key: str, field: str = "value"):
    """pl/bs/cf のどこかにある key の値を返す（無ければ None）。"""
    for sec in ("pl", "bs", "cf"):
        s = d.get(sec) or {}
        for coll in ("lines", "assets", "liabilities", "equity"):
            for ln in s.get(coll, []) or []:
                if ln.get("key") == key:
                    return D(ln.get(field))
        tot = (s.get("totals") or {}).get(key)
        if isinstance(tot, dict):
            return D(tot.get(field))
        if key in ("begin", "end") and sec == "cf" and isinstance(s.get(key), dict):
            return D(s[key].get(field))
    oe = (d.get("bs") or {}).get("owners_equity")
    if key == "owners_equity" and isinstance(oe, dict):
        return D(oe.get(field))
    return None


def period_days(d: dict) -> int | None:
    p = d.get("period", {})
    try:
        return (date.fromisoformat(p["end"]) - date.fromisoformat(p["start"])).days + 1
    except (KeyError, ValueError, TypeError):
        return None


def no_prior(lines: list) -> bool:
    """比較情報が1つもない（初めての連結決算など）。nil（「－」）の前期値は「ある」とみなす。"""
    return bool(lines) and all(ln.get("prior") is None and "prior" not in (ln.get("nil_fields") or []) for ln in lines)


def cmp_note(d: dict, what: str) -> str:
    r = (d.get("period") or {}).get("no_comparative_reason")
    return f"{what}の比較情報はない" + (f"（{r}）" if r else "") + "。"


def labels(d: dict) -> dict:
    fy = d.get("period", {}).get("type") == "FY"
    return {"prior_flow": "前期" if fy else "前年同期", "cur_flow": "当期" if fy else "当期累計",
            "prior_bs": "前期末", "cur_bs": "当期末" if fy else "当四半期末"}


# ======================================================================== 検証

class Report:
    def __init__(self):
        self.errors, self.warnings, self.checks = [], [], []

    def check(self, name, expected, actual, tol, where):
        e, a = D(expected), D(actual)
        if e is None or a is None:
            self.warnings.append(f"{where}: {name} は未開示の値を含むため照合を省略")
            return
        diff = a - e
        status = "ok" if diff == 0 else ("rounding" if abs(diff) <= tol else "mismatch")
        self.checks.append({"where": where, "check": name, "expected": str(e), "computed": str(a),
                            "diff": str(diff), "tolerance": str(tol), "status": status})
        if status == "mismatch":
            self.errors.append(f"{where}: {name} が一致しない（開示値 {e} / 計算値 {a} / 差 {diff} / 許容 {tol}）")


def _tol(n_items: int, basis: dict) -> Decimal:
    # 各行を独立に切捨て・四捨五入した値の合計は、項目数×1単位まで合計と食い違い得る
    return Decimal(max(1, n_items))


def validate(d: dict) -> dict:
    r = Report()
    basis = d.get("basis", {})
    for path in ("company.name", "company.code", "period.type", "period.start", "period.end",
                 "basis.standard", "basis.scope", "basis.unit"):
        cur = d
        for k in path.split("."):
            cur = cur.get(k) if isinstance(cur, dict) else None
        if cur in (None, ""):
            r.errors.append(f"必須項目 {path} が未設定")
    if d.get("period", {}).get("type") not in PERIOD_TYPES:
        r.errors.append(f"period.type は {sorted(PERIOD_TYPES)} のいずれか")
    if basis.get("standard") not in STANDARDS:
        r.errors.append(f"basis.standard は {sorted(STANDARDS)} のいずれか")
    if basis.get("scope") not in ("連結", "単体"):
        r.errors.append("basis.scope は 連結 / 単体")
    if not d.get("sources"):
        r.errors.append("sources（出典）が空。数値には出典が必要")
    src_ids = {s.get("id") for s in d.get("sources", [])}
    if d.get("sample"):
        r.warnings.append("sample=true: サンプルデータ。実企業の分析結果として扱わないこと")

    # ---- 出典参照
    def walk_src(lines, where):
        for ln in lines or []:
            s = ln.get("src")
            if ln.get("value") is not None and not s:
                r.warnings.append(f"{where}: 「{ln.get('label')}」に出典(src)がない")
            elif s and s.split()[0] not in src_ids:
                r.errors.append(f"{where}: 「{ln.get('label')}」の出典 {s} が sources に存在しない")

    pl, bs, cf = d.get("pl") or {}, d.get("bs") or {}, d.get("cf") or {}
    walk_src(pl.get("lines"), "PL")
    for side in ("assets", "liabilities", "equity"):
        walk_src(bs.get(side), f"BS.{side}")
    walk_src(cf.get("lines"), "CF")

    # ---- PL: 段階利益の連鎖
    for field in ("value", "prior"):
        lines = [ln for ln in pl.get("lines", []) if ln.get("kind") in CHAIN_KINDS and not ln.get("parent")]
        running, n, any_value = Decimal(0), 0, False
        for ln in lines:
            v = fv(ln, field)
            if ln["kind"] == "subtotal":
                if ln.get("check", True) and any_value:
                    if running is None:
                        r.warnings.append(f"PL({field}): {ln['label']} の前に未開示項目があり照合を省略")
                    else:
                        r.check(f"{ln['label']} = 直前までの収益−費用", v, running, _tol(n + 1, basis), f"PL({field})")
                running, n = v, 0
                any_value = any_value or v is not None
                continue
            if v is None:
                if field == "value":
                    r.warnings.append(f"PL: {ln['label']} が未開示（NULL）")
                running = None  # 未開示を 0 とみなさない。次の段階利益まで照合不能
                continue
            any_value = True
            if running is not None:
                running += SIGN[ln["kind"]] * v
            n += 1
        # 内訳 = 親
        by = lines_by_key(pl.get("lines", []))
        for key, parent in by.items():
            kids = [ln for ln in pl.get("lines", []) if ln.get("parent") == key]
            if kids and D(parent.get(field)) is not None:
                vals = [fv(k, field) for k in kids]
                if None in vals:
                    continue
                r.check(f"{parent['label']} = 内訳の合計", parent.get(field), sum(vals), _tol(len(kids), basis), f"PL({field})")
    for key in ("revenue_key", "split_after"):
        if pl.get(key) and pl[key] not in lines_by_key(pl.get("lines", [])):
            r.errors.append(f"pl.{key}={pl[key]} に対応する行がない")

    # ---- BS
    if bs:
        tot = bs.get("totals", {})
        for field in ("value", "prior"):
            def tv(k):
                t = tot.get(k)
                return D(t.get(field)) if isinstance(t, dict) else None
            ta, tl, tn, tln = tv("assets"), tv("liabilities"), tv("net_assets"), tv("liabilities_and_net_assets")
            if ta is not None and tln is not None:
                r.check("資産合計 = 負債純資産合計", ta, tln, Decimal(0), f"BS({field})")
            if tl is not None and tn is not None and ta is not None:
                r.check("資産合計 = 負債合計 + 純資産合計", ta, tl + tn, Decimal(1), f"BS({field})")
            for side, total_key in (("assets", "assets"), ("liabilities", "liabilities"), ("equity", "net_assets")):
                lines = [ln for ln in bs.get(side, []) if not ln.get("parent")]
                groups = {}
                for ln in lines:
                    groups.setdefault(ln.get("group") or side, []).append(ln)
                gt = bs.get("group_totals", {})
                gsum_all, ok_all = Decimal(0), True
                for g, lns in groups.items():
                    vals = [D(ln.get(field)) for ln in lns]
                    if None in vals:
                        ok_all = False
                        continue
                    s = sum(vals, Decimal(0))
                    gsum_all += s
                    gtot = gt.get(g, {}).get(field) if isinstance(gt.get(g), dict) else None
                    if gtot is not None:
                        r.check(f"{g}合計 = 各行の合計", gtot, s, _tol(len(lns), basis), f"BS({field})")
                if ok_all and tv(total_key) is not None:
                    r.check(f"{side} 合計 = 各行の合計", tv(total_key), gsum_all, _tol(len(lines), basis), f"BS({field})")
            for side in ("assets", "liabilities", "equity"):
                rows = bs.get(side, [])
                for par in [ln for ln in rows if ln.get("key")]:
                    kids = [ln for ln in rows if ln.get("parent") == par["key"]]
                    if kids and fv(par, field) is not None:
                        vals = [fv(k, field) for k in kids]
                        if None not in vals:
                            r.check(f"{par['label']} = 内訳の合計", fv(par, field), sum(vals, Decimal(0)),
                                    _tol(len(kids), basis), f"BS({field})")
            oe = (bs.get("owners_equity") or {}).get(field)
            if oe is not None and tn is not None and D(oe) > tn:
                nci = next((fv(ln, field) for ln in bs.get("equity", []) if ln.get("key") == "nci_equity"), None)
                if nci is not None and nci < 0 and abs(tn - D(oe) - nci) <= 1:
                    pass  # 非支配持分がマイナス（子会社の累積損失）なら、自己資本が純資産を上回るのは正常
                else:
                    r.errors.append(f"BS({field}): 自己資本 {oe} が純資産 {tn} を上回っている")

    # ---- CF
    if cf:
        if cf.get("disclosed") is False:
            if cf.get("lines"):
                r.errors.append("cf.disclosed=false なのに lines がある。未開示の数値を作らないこと")
            if not cf.get("reason_if_not"):
                r.warnings.append("cf.disclosed=false の理由（reason_if_not）を記載すること")
        else:
            for field in ("value", "prior"):
                b = D((cf.get("begin") or {}).get(field))
                e = D((cf.get("end") or {}).get(field))
                # 「－」（nil_fields）の欄は 0 として照合する（その期に該当する取引がない行）
                flows = [fv(ln, field) for ln in cf.get("lines", []) if not ln.get("parent") and ln.get("kind") != "info"]
                if b is None or e is None or not flows:
                    continue
                if None in flows:
                    r.warnings.append(f"CF({field}): 未開示の項目があり残高照合を省略")
                    continue
                r.check("期首残高 + 各CF + 換算差額等 = 期末残高", e, b + sum(flows), _tol(len(flows) + 1, basis), f"CF({field})")
            for field in ("value", "prior"):
                for par in [ln for ln in cf.get("lines", []) if ln.get("key") and not ln.get("parent")]:
                    kids = [ln for ln in cf.get("lines", []) if ln.get("parent") == par["key"] and ln.get("kind") != "subtotal"]
                    if not kids or fv(par, field) is None or par.get("check_children") is False:
                        continue
                    vals = [fv(k, field) for k in kids]
                    if None in vals:
                        continue
                    r.check(f"{par['label']} = 内訳の合計（小計行を除く）", fv(par, field), sum(vals, Decimal(0)),
                            _tol(len(kids), basis), f"CF({field})")
            keys = lines_by_key(cf.get("lines", []))
            for k in ("cfo", "cfi", "cff"):
                if k not in keys:
                    r.warnings.append(f"CF: 標準キー {k} の行がない")
            # BS の現金及び預金と CF の期末残高の差は定義差（預入期間3か月超の定期預金など）として注記を促す
            cash_bs = val(d, "cash")
            e = D((cf.get("end") or {}).get("value"))
            if cash_bs is not None and e is not None and cash_bs != e and not cf.get("cash_reconciliation_note"):
                r.warnings.append(f"BSの現金及び預金({cash_bs})とCF期末残高({e})が異なる。定義差を cf.cash_reconciliation_note に記載すること")

    # ---- 期間
    p = d.get("period", {})
    days = period_days(d)
    if days:
        exp = {"FY": (360, 371), "Q1": (88, 95), "Q2": (179, 186), "Q3": (270, 278)}.get(p.get("type"))
        if exp and not (exp[0] <= days <= exp[1]) and not p.get("irregular"):
            r.warnings.append(f"期間日数 {days} 日が {p.get('type')} の通常範囲外（変則決算なら period.irregular=true）")
    ok = not r.errors
    summary = {"ok": ok, "errors": r.errors, "warnings": r.warnings,
               "rounding_diffs": [c for c in r.checks if c["status"] == "rounding"],
               "n_checks": len(r.checks), "checks": r.checks}
    return summary


# ======================================================================== 指標

def _avg(cur, prior):
    if cur is None:
        return None, False
    if prior is None:
        return cur, False
    return (cur + prior) / 2, True


def growth(cur, prior):
    c, p = D(cur), D(prior)
    if c is None or p is None:
        return None, "未開示"
    if p == 0:
        return None, "前期ゼロ"
    if p < 0 and c >= 0:
        return None, "黒字転換"
    if p > 0 and c < 0:
        return None, "赤字転落"
    if p < 0 and c < 0:
        return None, "赤字縮小" if c > p else "赤字拡大"
    return round_half_up((c - p) / abs(p) * 100, 1), None


def metrics(d: dict) -> dict:
    out = []
    months = Decimal(d.get("period", {}).get("months") or 12)
    fy = d.get("period", {}).get("type") == "FY"
    ann = Decimal(12) / months
    days = Decimal(period_days(d) or 365)
    pl = d.get("pl") or {}
    rev_key = pl.get("revenue_key", "revenue")
    ni_key = pl.get("net_income_key", "net_income_parent")

    def add(key, label, value, unit, formula, cat, note=None, prior=None, annualized=False):
        out.append({"key": key, "label": label, "category": cat, "value": None if value is None else str(value),
                    "prior": None if prior is None else str(prior), "unit": unit, "formula": formula,
                    "annualized": annualized, "note": note})

    def ratio(n, dn, places=1):
        if n is None or dn in (None, 0):
            return None
        return round_half_up(n / dn * 100, places)

    rev, rev_p = val(d, rev_key), val(d, rev_key, "prior")
    # ---- 収益性（利益率）
    for key, label in (("gross_profit", "売上総利益率"), ("operating_income", "営業利益率"),
                       ("ordinary_income", "経常利益率"), ("pretax_income", "税引前利益率"),
                       (ni_key, "純利益率" if ni_key == "net_income"
                        else ("純利益率（親会社株主帰属）" if d.get("basis", {}).get("standard") != "IFRS"
                              else "純利益率（親会社所有者帰属）"))):
        v, vp = val(d, key), val(d, key, "prior")
        if v is None and vp is None:
            continue
        add(f"margin_{key}", label, ratio(v, rev), "%", f"{_lab(d, key)} ÷ {_lab(d, rev_key)} × 100", "収益性",
            prior=ratio(vp, rev_p))
    for key, label in (("cogs", "売上原価率"), ("sga", "販管費率")):
        v, vp = val(d, key), val(d, key, "prior")
        if v is not None:
            add(f"ratio_{key}", label, ratio(v, rev), "%", f"{_lab(d, key)} ÷ {_lab(d, rev_key)} × 100", "収益性",
                prior=ratio(vp, rev_p))
    tax, pti = val(d, "income_taxes"), val(d, "pretax_income")
    etr = ratio(tax, pti) if (pti and pti > 0 and tax is not None) else None
    if tax is not None:
        add("effective_tax_rate", "実効税率（実績）", etr, "%", "法人税等 ÷ 税引前利益 × 100", "収益性",
            note=None if etr is not None else "税引前利益が0以下のため算出しない")

    # ---- 成長性
    for ln in pl.get("lines", []):
        if (ln.get("parent") and ln.get("key") != ni_key) or ln.get("kind") == "info":
            continue   # IFRS の親会社帰属利益は当期利益の内訳だが、成長率は出す
        if ln.get("key") in (rev_key, "gross_profit", "operating_income", "ordinary_income", "pretax_income", ni_key):
            g, why = growth(ln.get("value"), ln.get("prior"))
            add(f"growth_{ln['key']}", f"{ln['label']} 前年同期比" if not fy else f"{ln['label']} 前期比",
                g, "%", "(当期 − 前期) ÷ |前期| × 100", "成長性", note=why)

    # ---- 安全性
    bs = d.get("bs") or {}
    ta, ta_p = val(d, "assets"), val(d, "assets", "prior")
    oe, oe_p = val(d, "owners_equity"), val(d, "owners_equity", "prior")
    ca, cl = val(d, "current_assets"), val(d, "current_liabilities")
    ca_p, cl_p = val(d, "current_assets", "prior"), val(d, "current_liabilities", "prior")
    if bs:
        add("equity_ratio", "自己資本比率", ratio(oe, ta), "%", "自己資本 ÷ 総資産 × 100（期末）", "安全性",
            prior=ratio(oe_p, ta_p))
        add("current_ratio", "流動比率", ratio(ca, cl), "%", "流動資産 ÷ 流動負債 × 100（期末）", "安全性",
            prior=ratio(ca_p, cl_p))
        inv, inv_p = val(d, "inventories"), val(d, "inventories", "prior")
        if inv is not None and ca is not None:
            add("quick_ratio_simple", "当座比率（簡易）", ratio(ca - inv, cl), "%",
                "(流動資産 − 棚卸資産) ÷ 流動負債 × 100（期末）", "安全性",
                prior=ratio(ca_p - inv_p, cl_p) if (ca_p is not None and inv_p is not None) else None,
                note="前払費用等も含むため厳密な当座資産ではない")
        debt, debt_p, debt_lines, lease = _debt(d)
        if debt_lines:
            add("interest_bearing_debt", "有利子負債", debt, d["basis"]["unit"],
                " + ".join(debt_lines) + "（期末）", "安全性", prior=debt_p,
                note=(f"リース負債 {fmt_num(lease)} は含めていない" if lease else None))
            cash, cash_p = val(d, "cash"), val(d, "cash", "prior")
            if cash is not None:
                cl = _lab(d, "cash")
                add("net_cash", "ネットキャッシュ", cash - debt, d["basis"]["unit"], f"{cl} − 有利子負債（期末）",
                    "安全性", prior=(cash_p - debt_p) if (cash_p is not None and debt_p is not None) else None,
                    note="現金及び預金には預入期間3か月超の定期預金を含む場合がある" if cl == "現金及び預金" else None)
            add("de_ratio", "D/Eレシオ", round_half_up(debt / oe, 2) if oe else None, "倍",
                "有利子負債 ÷ 自己資本（期末）", "安全性",
                prior=round_half_up(debt_p / oe_p, 2) if (oe_p and debt_p is not None) else None)
        elif bs.get("no_debt"):
            # 前期末の BS がない（初めての連結決算など）ときは前期の値を 0 と表示しない
            bs_prior = ((bs.get("totals") or {}).get("assets") or {}).get("prior") is not None
            add("interest_bearing_debt", "有利子負債", 0, d["basis"]["unit"], "BS に借入金・社債等の表示なし（bs.no_debt）",
                "安全性", prior=0 if bs_prior else None,
                note="無借金（BS 本表に有利子負債の科目がない）。「その他」に含まれる少額のリース負債等は把握できない")
            cash, cash_p = val(d, "cash"), val(d, "cash", "prior")
            if cash is not None:
                add("net_cash", "ネットキャッシュ", cash, d["basis"]["unit"], f"{_lab(d, 'cash')} − 有利子負債（0）（期末）", "安全性",
                    prior=cash_p, note="有価証券など現金同等の運用資産は含めていない")
            add("de_ratio", "D/Eレシオ", Decimal("0.00"), "倍", "有利子負債 ÷ 自己資本（期末）", "安全性",
                prior=Decimal("0.00") if bs_prior else None)
        else:
            add("interest_bearing_debt", "有利子負債", None, d["basis"]["unit"], "interest_bearing=true の負債の合計",
                "安全性", note="有利子負債の行が指定されていない（無借金の場合は bs.no_debt=true を設定）")

    # ---- 効率性・資本効率
    ni = val(d, ni_key)
    if bs and ni is not None:
        a_oe, both = _avg(oe, oe_p)
        a_ta, both2 = _avg(ta, ta_p)
        note_avg = "期首（前期末）と期末の平均" if both else "期末値のみ（期首値なし）"
        if fy:
            add("roe", "ROE（自己資本利益率）", ratio(ni, a_oe), "%",
                f"{_lab(d, ni_key)} ÷ 自己資本（{note_avg}） × 100", "効率性")
            add("roa", "ROA（総資産利益率）", ratio(ni, a_ta), "%",
                f"{_lab(d, ni_key)} ÷ 総資産（{note_avg}） × 100", "効率性")
        else:
            add("roe", "ROE（年換算・参考）", ratio(ni * ann, a_oe), "%",
                f"{_lab(d, ni_key)} × 12/{months} ÷ 自己資本（{note_avg}） × 100", "効率性", annualized=True,
                note="累計利益を単純に年換算。季節性がある事業では通期実績と大きく異なり得る")
            add("roa", "ROA（年換算・参考）", ratio(ni * ann, a_ta), "%",
                f"{_lab(d, ni_key)} × 12/{months} ÷ 総資産（{note_avg}） × 100", "効率性", annualized=True)
        if rev is not None and a_ta:
            add("asset_turnover", "総資産回転率" + ("" if fy else "（年換算・参考）"),
                round_half_up(rev * (1 if fy else ann) / a_ta, 2), "回",
                f"{_lab(d, rev_key)}{'' if fy else ' × 12/' + str(months)} ÷ 総資産（{note_avg}）", "効率性",
                annualized=not fy)
        op = val(d, "operating_income")
        debt, debt_p, debt_lines, _ = _debt(d)
        if op is not None and etr is not None and oe is not None and (debt_lines or bs.get("no_debt")):
            ic, ic_p = oe + (debt or 0), (oe_p + (debt_p or 0)) if oe_p is not None else None
            a_ic, _b = _avg(ic, ic_p)
            nopat = op * (1 - etr / 100) * (1 if fy else ann)
            add("roic", "ROIC（投下資本利益率）" + ("" if fy else "（年換算・参考）"), ratio(nopat, a_ic), "%",
                "営業利益 × (1 − 実効税率) ÷ (有利子負債 + 自己資本)（平均）× 100", "効率性", annualized=not fy,
                note="実効税率は当期実績値。リース負債は含めない（設定 debt_includes_lease）")
        for key, base_key, label, base_label in (("receivables", rev_key, "売上債権回転日数", "売上高"),
                                                ("inventories", "cogs", "棚卸資産回転日数", "売上原価"),
                                                ("payables", "cogs", "仕入債務回転日数", "売上原価")):
            x, xp, base = val(d, key), val(d, key, "prior"), val(d, base_key)
            if x is None or not base:
                continue
            ax, _b = _avg(x, xp)
            add(f"days_{key}", label, round_half_up(ax / base * days, 1), "日",
                f"{_lab(d, key)}（平均） ÷ {base_label} × 対象期間日数({days}日)", "効率性")

    # ---- CF
    cf = d.get("cf") or {}
    if cf and cf.get("disclosed", True) and cf.get("lines"):
        cfo, cfi = val(d, "cfo"), val(d, "cfi")
        cfo_p, cfi_p = val(d, "cfo", "prior"), val(d, "cfi", "prior")
        if cfo is not None and cfi is not None:
            add("fcf", "フリーキャッシュフロー", cfo + cfi, d["basis"]["unit"], "営業CF + 投資CF", "CF",
                prior=(cfo_p + cfi_p) if (cfo_p is not None and cfi_p is not None) else None)
        capex = D((cf.get("capex") or {}).get("value"))
        if cfo is not None and capex is not None:
            add("fcf_capex", "FCF（設備投資ベース・参考）", cfo - abs(capex), d["basis"]["unit"],
                f"営業CF − {(cf.get('capex') or {}).get('label', '設備投資')}", "CF")
        if cfo is not None and rev:
            add("cfo_margin", "営業CFマージン", ratio(cfo, rev), "%", f"営業CF ÷ {_lab(d, rev_key)} × 100", "CF")
        if cfo is not None and ni and ni > 0:
            add("cfo_to_ni", "営業CF ÷ 純利益", round_half_up(cfo / ni, 2), "倍",
                f"営業CF ÷ {_lab(d, ni_key)}", "CF", note="1倍を大きく下回る状態が続く場合、利益が現金化されていない可能性")
    elif cf.get("disclosed") is False:
        add("fcf", "フリーキャッシュフロー", None, d["basis"]["unit"], "営業CF + 投資CF", "CF",
            note="CF計算書が開示されていないため算出しない")

    # ---- 一時要因（one_off）を除いた段階利益（参考）
    offs = [ln for ln in pl.get("lines", []) if ln.get("one_off")]
    if offs:
        order = pl.get("lines", [])
        for key in ("operating_income", "ordinary_income", "pretax_income"):
            sub = next((ln for ln in order if ln.get("key") == key), None)
            if not sub:
                continue
            idx = order.index(sub)
            # 段階利益より前にある one_off 行（内訳行は親の位置で判定）
            def pos(ln):
                if ln.get("parent"):
                    par = next((x for x in order if x.get("key") == ln["parent"]), None)
                    return order.index(par) if par else order.index(ln)
                return order.index(ln)
            used = [ln for ln in offs if pos(ln) < idx]
            if not used:
                continue
            def adj(field):
                base = fv(sub, field)
                if base is None:
                    return None
                eff = Decimal(0)
                for ln in used:
                    v = fv(ln, field)
                    if v is None:
                        continue
                    eff += v if ln["kind"] in ("revenue", "income") else -v
                return base - eff
            add(f"adj_{key}", f"{sub['label']}（一時要因を除く・参考）", adj("value"), d["basis"]["unit"],
                f"{sub['label']} − 一時要因（" + "、".join(ln["label"] for ln in used) + "）の影響", "収益性",
                prior=adj("prior"), note="one_off=true の行を除いた筆者計算。税金への影響は調整していない")

    # ---- 会社予想に対する進捗率
    fc = d.get("forecast") or {}
    if fc and not fy:
        for key in ("revenue", "operating_income", "ordinary_income", "net_income_parent", "net_income"):
            f, a = D(fc.get(key)), val(d, key if key != "revenue" else rev_key)
            if f and a is not None:
                nm = _fc_name(d, key, rev_key)
                note = f"通期予想は {fc.get('as_of', '公表日不明')} 時点"
                if f < 0 or a < 0:
                    note += "。予想または実績が損失のため、比率は「損失額の消化率」であり利益の進捗ではない" if (f < 0 and a < 0) \
                        else "。予想と実績の符号が異なるため進捗率は意味を持たない"
                add(f"progress_{key}", f"通期予想に対する進捗率（{nm}）", ratio(a, f) if (f > 0) == (a >= 0) else None, "%",
                    f"{nm}の累計実績 ÷ 通期会社予想 × 100", "成長性", note=note)
    return {"company": d.get("company", {}).get("name"), "period": d.get("period", {}).get("label"),
            "unit": d.get("basis", {}).get("unit"), "metrics": out}


def _fc_name(d, key, rev_key):
    """会社予想の項目名。net_income は非連結の当期純利益（IFRS は当期利益）。"""
    j = d["basis"]["standard"] == "J-GAAP"
    return {"revenue": _lab(d, rev_key), "operating_income": "営業利益", "ordinary_income": "経常利益",
            "net_income_parent": "親会社株主に帰属する当期純利益" if j else "親会社の所有者に帰属する当期利益",
            "net_income": "当期純利益" if j else "当期利益"}[key]


def _lab(d, key):
    for sec in ("pl", "bs", "cf"):
        s = d.get(sec) or {}
        for coll in ("lines", "assets", "liabilities", "equity"):
            for ln in s.get(coll, []) or []:
                if ln.get("key") == key:
                    return ln.get("label", key)
    return {"assets": "総資産", "owners_equity": "自己資本", "current_assets": "流動資産",
            "current_liabilities": "流動負債", "revenue": "売上高", "operating_income": "営業利益",
            "ordinary_income": "経常利益", "net_income_parent": "親会社株主に帰属する当期純利益"}.get(key, key)


def _debt(d):
    from fra_common import load_config
    inc_lease = load_config().get("debt_includes_lease", False)
    bs = d.get("bs") or {}
    total, total_p, labels_, lease = Decimal(0), Decimal(0), [], Decimal(0)
    has_p = True
    for ln in (bs.get("liabilities") or []):
        if not ln.get("interest_bearing"):
            continue
        # 「－」（nil_fields、全額返済・償還済みなど）は 0 として扱う。当期が「－」でも前期の残高は前期の合計に入れる
        v, vp = fv(ln), fv(ln, "prior")
        if ln.get("lease") and not inc_lease:
            lease += v or 0
            continue
        if v is None:
            continue
        total += v
        labels_.append(ln["label"] if ln["label"] not in labels_ else f"{ln['label']}（{ln.get('group', '')}）")
        if vp is None:
            has_p = False
        else:
            total_p += vp
    for ln in (bs.get("debt_breakdown") or []):   # 「その他」に含まれる借入金などを別記するとき
        v, vp = fv(ln), fv(ln, "prior")
        if v is None:
            continue
        total += v
        labels_.append(ln["label"])
        if vp is None:
            has_p = False
        else:
            total_p += vp
    return (total if labels_ else None), (total_p if (labels_ and has_p) else None), labels_, lease


# ======================================================================== Markdown 表

def _row(cells):
    return "| " + " | ".join(cells) + " |"


def table_pl(d: dict) -> str:
    pl, u, lb = d["pl"], d["basis"]["unit"], labels(d)
    rev, rev_p = val(d, pl.get("revenue_key", "revenue")), val(d, pl.get("revenue_key", "revenue"), "prior")
    np_ = no_prior(pl["lines"])
    out = [f"<!-- fra:table:pl -->",
           f"単位：{u}（{d['basis'].get('rounding', '端数処理は出典に従う')}）。構成比は売上高に対する比率。"
           + (cmp_note(d, lb["prior_flow"]) if np_ else ""),
           "",
           _row(["項目", lb["prior_flow"], f"**{lb['cur_flow']}**", "増減", "増減率", "売上高比"]),
           _row(["---", "--:", "--:", "--:", "--:", "--:"])]
    for ln in pl["lines"]:
        if ln.get("kind") == "info" and not ln.get("show"):
            continue
        v, p = fv(ln, "value"), fv(ln, "prior")
        diff = (v - p) if (v is not None and p is not None) else None
        g, why = growth(v, p)
        if (v == 0 or p == 0) and ln.get("nil_fields"):
            g, why = None, "―"
        elif ln["kind"] != "subtotal" and why in ("黒字転換", "赤字転落", "赤字縮小", "赤字拡大"):
            why = "符号反転" if why in ("黒字転換", "赤字転落") else "―"
        name = ("　" + ln["label"]) if ln.get("parent") else (f"**{ln['label']}**" if ln["kind"] == "subtotal" else ln["label"])
        out.append(_row([name, "―" if np_ else cell(ln, "prior"),
                         f"**{cell(ln, 'value')}**" if ln["kind"] == "subtotal" else cell(ln, "value"),
                         fmt_num(diff, plus=True) if diff is not None else "―",
                         "―" if np_ else (fmt_pct(g, plus=True) if g is not None else (why or "―")),
                         "―" if (v == 0 and ln.get("nil_fields")) else (fmt_pct(D(v) / rev * 100) if (v is not None and rev) else "―")]))
    out.append("<!-- /fra:table:pl -->")
    return "\n".join(out)


def table_bs(d: dict) -> str:
    bs, u, lb = d["bs"], d["basis"]["unit"], labels(d)
    ta = val(d, "assets")
    np_ = no_prior([ln for side in ("assets", "liabilities", "equity") for ln in bs.get(side, [])])
    dates = (f"{lb['cur_bs']}＝{bs.get('date', '')}。" + cmp_note(d, lb["prior_bs"])) if np_ else \
        f"{lb['prior_bs']}＝{bs.get('prior_date', '')}、{lb['cur_bs']}＝{bs.get('date', '')}。"
    def pc(x):   # 前期末の欄
        return "―" if np_ else fmt_num(x)

    def dc(x):   # 増減の欄
        return "―" if np_ else fmt_num(x, plus=True)

    out = ["<!-- fra:table:bs -->",
           f"単位：{u}。{dates}構成比は総資産に対する比率。",
           "", _row(["区分", "項目", lb["prior_bs"], f"**{lb['cur_bs']}**", "増減", "構成比"]),
           _row(["---", "---", "--:", "--:", "--:", "--:"])]
    gt = bs.get("group_totals", {})
    for side, side_label in (("assets", "資産"), ("liabilities", "負債"), ("equity", "純資産")):
        last_group = None
        keyed = {x["key"]: x for x in bs.get(side, []) if x.get("key")}

        def depth(x):   # 内訳の深さ（間接控除方式の取得原価・累計額は純額の内訳＝深さ2）
            n_, cur = 0, x
            while cur.get("parent") and cur["parent"] in keyed and n_ < 5:
                n_ += 1
                cur = keyed[cur["parent"]]
            return n_ if n_ else (1 if x.get("parent") else 0)
        for ln in bs.get(side, []):
            g = ln.get("group") or side_label
            if g != last_group and g in gt:
                t = gt[g]
                out.append(_row([f"**{g}**", "（合計）", pc(t.get("prior")), f"**{fmt_num(t.get('value'))}**",
                                 dc(_diff(t.get("value"), t.get("prior"))),
                                 fmt_pct(D(t.get("value")) / ta * 100) if (ta and t.get("value") is not None) else "―"]))
            last_group = g
            v, p = fv(ln), fv(ln, "prior")   # 「－」（nil_fields）は 0 として増減を計算し、表示は「－」
            mark = "（有利子）" if ln.get("interest_bearing") else ""
            name = "　" * depth(ln) + ln["label"] + mark
            out.append(_row([g if g not in gt else "", name, "―" if np_ else cell(ln, "prior"), cell(ln),
                             dc(_diff(v, p)),
                             fmt_pct(v / ta * 100) if (ta and v is not None and ln.get("value") is not None) else "―"]))
    ifrs = d["basis"].get("standard") == "IFRS"
    for key, label in (("assets", "資産合計"), ("liabilities", "負債合計"),
                       ("net_assets", "資本合計" if ifrs else "純資産合計"),
                       ("liabilities_and_net_assets", "負債及び資本合計" if ifrs else "負債純資産合計")):
        t = (bs.get("totals") or {}).get(key)
        if isinstance(t, dict):
            label = t.get("label") or label
            out.append(_row(["", f"**{label}**", pc(t.get("prior")), f"**{fmt_num(t.get('value'))}**",
                             dc(_diff(t.get("value"), t.get("prior"))), ""]))
    oe = bs.get("owners_equity")
    if oe:
        out.append(_row(["", f"（参考）{oe.get('label', '自己資本')}", pc(oe.get("prior")), fmt_num(oe.get("value")),
                         dc(_diff(oe.get("value"), oe.get("prior"))), ""]))
    out.append("<!-- /fra:table:bs -->")
    return "\n".join(out)


def _diff(a, b):
    a, b = D(a), D(b)
    return (a - b) if (a is not None and b is not None) else None


def table_cf(d: dict) -> str:
    cf, u, lb = d.get("cf") or {}, d["basis"]["unit"], labels(d)
    if not cf or cf.get("disclosed") is False:
        return ("<!-- fra:table:cf -->\n> [!note] キャッシュ・フロー計算書は未開示\n> "
                + (cf.get("reason_if_not") or "今回の資料にはCF計算書が含まれていない。")
                + "\n> 数値を推定・補完していない。\n<!-- /fra:table:cf -->")
    out = ["<!-- fra:table:cf -->", f"単位：{u}。対象：{cf.get('cash_definition', '現金及び現金同等物')}。", "",
           _row(["項目", lb["prior_flow"], f"**{lb['cur_flow']}**", "増減"]), _row(["---", "--:", "--:", "--:"])]
    b = cf.get("begin") or {}
    out.append(_row([b.get("label", "期首残高"), fmt_num(b.get("prior")), fmt_num(b.get("value")),
                     fmt_num(_diff(b.get("value"), b.get("prior")), plus=True)]))
    for ln in cf.get("lines", []):
        v, p = fv(ln, "value"), fv(ln, "prior")
        name = ("　" + ln["label"]) if ln.get("parent") else ln["label"]
        if ln.get("kind") == "subtotal":
            name = f"　**{ln['label']}**"
        out.append(_row([name, cell(ln, "prior"), f"**{cell(ln, 'value')}**" if ln.get("key") in ("cfo", "cfi", "cff") else cell(ln, "value"),
                         fmt_num(_diff(v, p), plus=True) if _diff(v, p) is not None else "―"]))
    e = cf.get("end") or {}
    out.append(_row([f"**{e.get('label', '期末残高')}**", fmt_num(e.get("prior")), f"**{fmt_num(e.get('value'))}**",
                     fmt_num(_diff(e.get("value"), e.get("prior")), plus=True)]))
    m = {x["key"]: x for x in metrics(d)["metrics"]}
    if "fcf" in m:
        out.append(_row(["（筆者計算）フリーキャッシュフロー＝営業CF＋投資CF", fmt_num(m["fcf"]["prior"]),
                         fmt_num(m["fcf"]["value"]), fmt_num(_diff(m["fcf"]["value"], m["fcf"]["prior"]), plus=True)]))
    out.append("<!-- /fra:table:cf -->")
    return "\n".join(out)


def table_metrics(d: dict) -> str:
    m = metrics(d)["metrics"]
    lb = labels(d)
    out = ["<!-- fra:table:metrics -->", "すべて筆者計算。定義列の式で算出。", "",
           _row(["観点", "指標", lb["prior_flow"], f"**{lb['cur_flow']}**", "定義・注記"]),
           _row(["---", "---", "--:", "--:", "---"])]
    for x in m:
        if x["category"] == "成長性" and x["key"].startswith("growth_"):
            continue  # 成長率は PL 表の増減率列で示す
        unit = x["unit"]

        def f(v):
            if v is None:
                return "―"
            if unit == "%":
                return fmt_pct(v)
            if unit in ("倍", "回"):
                return fmt_num(v, 2) + unit
            if unit == "日":
                return fmt_num(v, 1) + unit
            return fmt_num(v)
        note = x["formula"] + (f"。{x['note']}" if x.get("note") else "")
        out.append(_row([x["category"], x["label"], f(D(x["prior"])) if x["prior"] else "―",
                         f"**{f(D(x['value']))}**" if x["value"] else ("算出不可" if x.get("note") else "―"), note]))
    out.append("<!-- /fra:table:metrics -->")
    return "\n".join(out)


def table_segments(d: dict) -> str:
    segs = d.get("segments") or []
    if not segs and d.get("revenue_breakdown"):
        lb = labels(d)
        u = d.get("revenue_breakdown_unit") or d["basis"]["unit"]   # 内訳だけ別の単位で開示されている場合
        rb = d["revenue_breakdown"]
        tot = sum((D(x.get("value")) or 0) for x in rb)
        unote = "" if u == d["basis"]["unit"] else "（財務諸表とは単位が異なり、端数処理のため合計が損益計算書の売上高と一致しない場合がある）"
        out = ["<!-- fra:table:segments -->", f"単一セグメントのため売上高の内訳を示す。単位：{u}{unote}。", "",
               _row(["区分", lb["prior_flow"], f"**{lb['cur_flow']}**", "増減率", "構成比", "出典"]),
               _row(["---", "--:", "--:", "--:", "--:", "---"])]
        for x in rb:
            v, pv = D(x.get("value")), D(x.get("prior"))
            g, why = growth(v, pv)
            # 期全体に比較情報がない（no_comparative_reason あり）ときだけ「―」。内訳だけ前期が未開示なら「未開示」
            npb = all(D(y.get("prior")) is None for y in rb) and bool((d.get("period") or {}).get("no_comparative_reason"))
            out.append(_row([x["label"], "―" if npb else fmt_num(pv), fmt_num(v),
                             "―" if npb else (fmt_pct(g, plus=True) if g is not None else (why or "―")),
                             fmt_pct(v / tot * 100) if (v is not None and tot) else "―", x.get("src", "")]))
        out.append("<!-- /fra:table:segments -->")
        return "\n".join(out)
    if not segs:
        return "<!-- fra:table:segments -->\nセグメント情報の記載なし（単一セグメント、または資料に記載なし）。\n<!-- /fra:table:segments -->"
    lb = labels(d)
    u = d["basis"]["unit"]
    tot = sum((D(s.get("revenue")) or 0) for s in segs)
    rt_note = "利益率はセグメント間の内部売上を含むセグメント売上計に対する比率。" if any(x.get("revenue_total") for x in segs) else "利益率は表示した売上に対する比率。"
    out = ["<!-- fra:table:segments -->", f"単位：{u}。売上は表示値（外部顧客向けなど、出典の区分に従う）。構成比は表示した売上の合計に対する比率。{rt_note}", "",
           _row(["セグメント", f"売上 {lb['prior_flow']}", f"売上 **{lb['cur_flow']}**", "増減率", "売上構成比",
                 f"利益 {lb['prior_flow']}", f"利益 **{lb['cur_flow']}**", "利益率", f"利益率（{lb['prior_flow']}）"]),
           _row(["---", "--:", "--:", "--:", "--:", "--:", "--:", "--:", "--:"])]
    for s in segs:
        r, rp, pf, pp = D(s.get("revenue")), D(s.get("revenue_prior")), D(s.get("profit")), D(s.get("profit_prior"))
        g, why = growth(r, rp)
        rt, rtp = D(s.get("revenue_total")) or r, D(s.get("revenue_total_prior")) or rp
        g2, why2 = growth(pf, pp)
        out.append(_row([s["name"], fmt_num(rp), fmt_num(r), fmt_pct(g, plus=True) if g is not None else (why or "―"),
                         fmt_pct(r / tot * 100) if (r is not None and tot) else "―", fmt_num(pp), fmt_num(pf),
                         fmt_pct(pf / rt * 100) if (pf is not None and rt) else "―",
                         fmt_pct(pp / rtp * 100) if (pp is not None and rtp) else "―"]))
    out.append("<!-- /fra:table:segments -->")
    return "\n".join(out)


def table_forecast(d: dict) -> str:
    fc = d.get("forecast") or {}
    if not fc:
        return "<!-- fra:table:forecast -->\n会社予想の記載なし。\n<!-- /fra:table:forecast -->"
    u = d["basis"]["unit"]
    fy = d["period"]["type"] == "FY"
    if fy:
        status = "本決算と同時に公表された次期の予想。"
        head = ["項目", "次期予想", "当期実績", "当期実績比（会社開示）"]
    else:
        status = "今回修正あり。" if fc.get("revised") else "前回公表から修正なし。"
        head = ["項目", "通期予想", "前期実績比（会社開示）", "当期累計実績", "進捗率"]
    if fc.get("undetermined"):
        # 会社が業績予想を「未定」とした場合（連結移行・M&A・災害など）。数値を補わない
        status = "会社は業績予想を「未定」としている。" + (f"{fc['undetermined_reason']}。" if fc.get("undetermined_reason") else "")
    out = ["<!-- fra:table:forecast -->", f"会社予想（{fc.get('period', '')}、{fc.get('as_of', '')} 時点、単位：{u}）。{status}",
           "", _row(head), _row(["---"] + ["--:"] * (len(head) - 1))]
    for key in ("revenue", "operating_income", "ordinary_income", "net_income_parent", "net_income"):
        f = D(fc.get(key))
        if f is None:
            continue
        a = val(d, key if key != "revenue" else d["pl"].get("revenue_key", "revenue"))
        chg = fc.get(f"{key}_change_pct")
        name = _fc_name(d, key, d["pl"].get("revenue_key", "revenue"))
        chg_s = fmt_pct(D(chg), plus=True) if chg is not None else "―"
        if fy:
            out.append(_row([name, fmt_num(f), fmt_num(a), chg_s]))
        else:
            out.append(_row([name, fmt_num(f), chg_s, fmt_num(a), fmt_pct(a / f * 100) if (a is not None and f) else "―"]))
    for k, name in (("eps", "1株当たり当期純利益（円）"), ("dividend", "1株当たり年間配当（円）")):
        if fc.get(k) is not None:
            out.append(_row([name, fmt_num(D(fc[k]), 2)] + ["―"] * (len(head) - 2)))
    out.append("<!-- /fra:table:forecast -->")
    return "\n".join(out)


TABLES = {"pl": table_pl, "bs": table_bs, "cf": table_cf, "metrics": table_metrics,
          "segments": table_segments, "forecast": table_forecast}


# ======================================================================== 単独四半期

def quarter(cur: dict, prev: dict) -> dict:
    """累計値の差分で単独四半期を出す。比較可能性を先に確認する。"""
    problems = []
    for path in (("basis", "standard"), ("basis", "scope"), ("basis", "unit"), ("company", "code")):
        a, b = cur[path[0]].get(path[1]), prev[path[0]].get(path[1])
        if a != b:
            problems.append(f"{'.'.join(path)} が異なる（{a} / {b}）")
    if cur["period"]["start"] != prev["period"]["start"]:
        problems.append("累計期間の開始日が異なる（同一会計年度ではない）")
    if prev["period"]["end"] >= cur["period"]["end"]:
        problems.append("前四半期の終了日が当期以降")
    if cur.get("period", {}).get("restated") or prev.get("period", {}).get("restated"):
        problems.append("遡及修正・組替えがあるため単純差分は不可（修正後の前四半期値を使うこと）")
    if problems:
        return {"ok": False, "problems": problems}
    pk = lines_by_key(prev["pl"]["lines"])
    rows = []
    for ln in cur["pl"]["lines"]:
        p = pk.get(ln.get("key"))
        v, pv = D(ln.get("value")), D(p.get("value")) if p else None
        rows.append({"key": ln.get("key"), "label": ln["label"],
                     "standalone": str(v - pv) if (v is not None and pv is not None) else None,
                     "standalone_prior_year": str(D(ln.get("prior")) - D(p.get("prior")))
                     if (p and ln.get("prior") is not None and p.get("prior") is not None) else None})
    return {"ok": True, "period": f"{prev['period']['end']} 翌日 〜 {cur['period']['end']}",
            "note": "累計値の差分。各累計値が切捨て表示のため ±1 単位の誤差を含み得る。", "pl": rows}


# ======================================================================== 履歴比較

def history(data_dir: str) -> dict:
    files = sorted(Path(data_dir).glob("*.json"))
    periods = []
    for f in files:
        try:
            d = load(f)
        except json.JSONDecodeError:
            continue
        if "period" not in d:
            continue
        m = {x["key"]: x for x in metrics(d)["metrics"]}
        periods.append({"file": f.name, "label": d["period"].get("label"), "type": d["period"]["type"],
                        "end": d["period"]["end"], "standard": d["basis"]["standard"], "scope": d["basis"]["scope"],
                        "unit": d["basis"]["unit"], "sample": bool(d.get("sample")),
                        "revenue": str(val(d, d["pl"].get("revenue_key", "revenue"))),
                        "operating_income": str(val(d, "operating_income")),
                        "net_income_parent": str(val(d, d["pl"].get("net_income_key", "net_income_parent"))),
                        "operating_margin": m.get("margin_operating_income", {}).get("value"),
                        "equity_ratio": m.get("equity_ratio", {}).get("value"),
                        "forecast": d.get("forecast"), "watch_next": d.get("watch_next", []),
                        "concerns": d.get("concerns", [])})
    periods.sort(key=lambda p: (p["end"], p["type"]))
    notes = []
    stds = {p["standard"] for p in periods}
    if len(stds) > 1:
        notes.append(f"会計基準が期によって異なる（{sorted(stds)}）。基準をまたぐ比較は参考扱い")
    if len({p["scope"] for p in periods}) > 1:
        notes.append("連結と単体が混在している。比較しないこと")
    # 会社予想と実績の差: 各本決算について、同じ会計年度を対象とした過去の予想を集める
    fc_vs = []
    for p in periods:
        if p["type"] != "FY":
            continue
        fy = p["label"].split("_")[0]
        for q in periods:
            fc = q.get("forecast") or {}
            if q is p or fc.get("period") != fy:
                continue
            for key in ("revenue", "operating_income", "net_income_parent", "net_income"):
                pk = "net_income_parent" if key == "net_income" else key   # 履歴の純利益は net_income_key の値
                if fc.get(key) is None or p.get(pk) in (None, "None"):
                    continue
                a, f = D(p[pk]), D(fc[key])
                fc_vs.append({"fiscal_year": fy, "forecast_in": q["label"], "as_of": fc.get("as_of"), "item": key,
                              "forecast": str(f), "actual": str(a), "diff": str(a - f),
                              "achievement_pct": str(round_half_up(a / f * 100, 1)) if f else None})
    return {"periods": periods, "comparability_notes": notes, "forecast_vs_actual": fc_vs}


# ======================================================================== 計算（暗算を避けるため）

def calc(expr: str, places: int | None = None) -> dict:
    """四則演算と括弧だけを Decimal で評価する（eval は使わない）。"""
    import ast
    tree = ast.parse(expr.replace(",", "").replace("×", "*").replace("÷", "/").replace("−", "-"), mode="eval")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return Decimal(str(n.value))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = ev(n.left), ev(n.right)
            return {ast.Add: a + b, ast.Sub: a - b, ast.Mult: a * b}.get(type(n.op)) if not isinstance(n.op, ast.Div) else a / b
        raise ValueError("使える演算は + - * / と括弧だけ")
    v = ev(tree)
    return {"expr": expr, "value": str(v), "rounded": str(round_half_up(v, places)) if places is not None else None}


# ======================================================================== CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for c in ("validate", "metrics"):
        x = sp.add_parser(c)
        x.add_argument("data")
    t = sp.add_parser("tables")
    t.add_argument("data")
    t.add_argument("--only", default="pl,bs,cf,metrics,segments,forecast")
    q = sp.add_parser("quarter")
    q.add_argument("cur")
    q.add_argument("prev")
    h = sp.add_parser("history")
    h.add_argument("data_dir")
    c = sp.add_parser("calc", help='例: calc "(814570-539804)/539804*100" --places 1')
    c.add_argument("expr")
    c.add_argument("--places", type=int)
    a = ap.parse_args()
    if a.cmd == "validate":
        res = validate(load(a.data))
        emit(res)
        sys.exit(0 if res["ok"] else 1)
    if a.cmd == "metrics":
        emit(metrics(load(a.data)))
    elif a.cmd == "tables":
        d = load(a.data)
        for k in a.only.split(","):
            if k == "bs" and not d.get("bs"):
                continue
            print(TABLES[k](d))
            print()
    elif a.cmd == "quarter":
        emit(quarter(load(a.cur), load(a.prev)))
    elif a.cmd == "history":
        emit(history(a.data_dir))
    elif a.cmd == "calc":
        emit(calc(a.expr, a.places))


if __name__ == "__main__":
    main()
