#!/usr/bin/env python3
"""TDnet の決算短信 XBRL zip から、決算データ JSON の「下書き」を作る。

- 数値は XBRL の値をそのまま使う（転記ミスを防ぐ）。
- 区分（売上・費用・段階利益・内訳）、BS のグループ、有利子負債の判定は要素名から推定する。
  推定できない行は kind="unknown" とし、fin.py validate がエラーにする。人（Claude）が確認して直す。
- 会社の業種、出典の頁番号、セグメント、強調表示などは下書き作成後に追記する。

使い方:
  python3 -I xbrl_to_data.py <xbrl.zip> --pdf-url <短信PDFのURL> [--xbrl-url <zipのURL>] --out <data.json>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xbrl_facts import extract  # noqa: E402

# ------------------------------------------------------------------ PL 要素 → (key, kind)
PL_MAP = {
    # 日本基準
    "NetSales": ("revenue", "revenue"), "OperatingRevenue1": ("revenue", "revenue"),
    "OperatingRevenue2": ("revenue", "revenue"), "Revenue": ("revenue", "revenue"),
    "NetSalesOfCompletedConstructionContracts": ("revenue", "revenue"),
    "CostOfSales": ("cogs", "expense"), "OperatingCost": ("cogs", "expense"),
    "CostOfSalesOfCompletedConstructionContracts": ("cogs", "expense"),
    "GrossProfit": ("gross_profit", "subtotal"),
    "SellingGeneralAndAdministrativeExpenses": ("sga", "expense"),
    "OperatingExpenses": ("operating_expenses", "expense"),
    "OperatingIncome": ("operating_income", "subtotal"),
    "NonOperatingIncome": ("non_op_income", "income"),
    "NonOperatingExpenses": ("non_op_expense", "expense"),
    "OrdinaryIncome": ("ordinary_income", "subtotal"),
    "ExtraordinaryIncome": ("extraordinary_income", "income"),
    "ExtraordinaryLoss": ("extraordinary_loss", "expense"),
    "IncomeBeforeIncomeTaxes": ("pretax_income", "subtotal"),
    "IncomeTaxes": ("income_taxes", "expense"),
    "ProfitLoss": ("net_income", "subtotal"),
    "ProfitLossAttributableToNonControllingInterests": ("nci", "attribution"),
    "ProfitLossAttributableToOwnersOfParent": ("net_income_parent", "subtotal"),
    # IFRS
    "RevenueIFRS": ("revenue", "revenue"), "NetSalesIFRS": ("revenue", "revenue"),
    "CostOfSalesIFRS": ("cogs", "expense"), "GrossProfitIFRS": ("gross_profit", "subtotal"),
    "SellingGeneralAndAdministrativeExpensesIFRS": ("sga", "expense"),
    "ResearchAndDevelopmentExpenditureRecognizedAsExpenseDuringPeriodIFRS": ("rnd", "expense"),
    "ResearchAndDevelopmentExpensesIFRS": ("rnd", "expense"),
    "OtherIncomeIFRS": ("other_income", "income"), "OtherExpensesIFRS": ("other_expense", "expense"),
    "OtherOperatingIncomeIFRS": ("other_income", "income"),
    "OtherNonOperatingIncomeAndExpensesIFRS": ("other_non_operating", "income"),   # その他の営業外損益（△は損失）
    "OtherOperatingExpensesIFRS": ("other_expense", "expense"),
    "OperatingProfitLossIFRS": ("operating_income", "subtotal"),
    "FinanceIncomeIFRS": ("finance_income", "income"), "FinanceCostsIFRS": ("finance_costs", "expense"),
    "ShareOfProfitLossOfInvestmentsAccountedForUsingEquityMethodIFRS": ("equity_method", "income"),
    "ProfitLossBeforeTaxIFRS": ("pretax_income", "subtotal"),
    "IncomeTaxExpenseIFRS": ("income_taxes", "expense"),
    "ProfitLossFromContinuingOperationsIFRS": ("net_income_continuing", "subtotal"),
    "ProfitLossFromDiscontinuedOperationsIFRS": ("discontinued", "income"),
    "ProfitLossIFRS": ("net_income", "subtotal"),
    "ProfitLossAttributableToOwnersOfParentIFRS": ("net_income_parent", "subtotal"),
    "ProfitLossAttributableToNonControllingInterestsIFRS": ("nci", "attribution"),
}
PL_CHILD_SUFFIX = {"NOI": "non_op_income", "NOE": "non_op_expense", "EI": "extraordinary_income",
                   "EL": "extraordinary_loss"}
PL_TAX_CHILDREN = {"IncomeTaxesCurrent", "IncomeTaxesDeferred", "IncomeTaxesForPriorPeriods"}
INFO_RE = re.compile(r"(OCI|ComprehensiveIncome|OtherComprehensiveIncome)")   # 包括利益の部分は参考行（計算に使わない）
IFRS_ATTRIB = {"ProfitLossAttributableToOwnersOfParentIFRS", "ProfitLossAttributableToNonControllingInterestsIFRS"}

# ------------------------------------------------------------------ BS
BS_TOTALS = {
    "CurrentAssets": "current_assets", "NoncurrentAssets": "noncurrent_assets", "DeferredAssets": "deferred_assets",
    "Assets": "assets", "CurrentLiabilities": "current_liabilities", "NoncurrentLiabilities": "noncurrent_liabilities",
    "Liabilities": "liabilities", "NetAssets": "net_assets", "LiabilitiesAndNetAssets": "liabilities_and_net_assets",
    "CurrentAssetsIFRS": "current_assets", "NonCurrentAssetsIFRS": "noncurrent_assets", "AssetsIFRS": "assets",
    "TotalCurrentLiabilitiesIFRS": "current_liabilities", "NonCurrentLiabilitiesIFRS": "noncurrent_liabilities",
    "NonCurrentLabilitiesIFRS": "noncurrent_liabilities",   # TDnet タクソノミの綴り（Labilities）に合わせる
    "LiabilitiesIFRS": "liabilities", "EquityIFRS": "net_assets", "LiabilitiesAndEquityIFRS": "liabilities_and_net_assets",
}
GROUP_OF_TOTAL = {"current_assets": ("assets", "流動資産"), "noncurrent_assets": ("assets", "固定資産"),
                  "deferred_assets": ("assets", "繰延資産"), "current_liabilities": ("liabilities", "流動負債"),
                  "noncurrent_liabilities": ("liabilities", "固定負債"), "net_assets": ("equity", "純資産")}
BS_SUBGROUPS = {"PropertyPlantAndEquipment": "ppe", "IntangibleAssets": "intangibles",
                "InvestmentsAndOtherAssets": "investments_other", "ShareholdersEquity": "shareholders_equity",
                "ValuationAndTranslationAdjustments": "aoci",
                "EquityAttributableToOwnersOfParentIFRS": "owners_equity_ifrs"}
BS_KEYS = {
    "CashAndDeposits": "cash", "CashAndCashEquivalentsIFRS": "cash",
    "NotesAndAccountsReceivableTrade": "receivables", "AccountsReceivableTrade": "receivables",
    "NotesAndAccountsReceivableTradeAndContractAssets": "receivables",
    "AccountsReceivableTradeAndContractAssets": "receivables", "TradeAndOtherReceivablesCAIFRS": "receivables",
    "Inventories": "inventories", "InventoriesIFRS": "inventories", "InventoriesCAIFRS": "inventories",
    "GoodwillAndIntangibleAssetsIFRS": "intangibles",
    "NotesAndAccountsPayableTrade": "payables", "AccountsPayableTrade": "payables",
    "TradeAndOtherPayablesCLIFRS": "payables",
    "PropertyPlantAndEquipment": "ppe", "PropertyPlantAndEquipmentIFRS": "ppe",
    "IntangibleAssets": "intangibles", "IntangibleAssetsIFRS": "intangibles", "GoodwillIFRS": "goodwill",
    "Goodwill": "goodwill", "InvestmentSecurities": "investment_securities",
    "NonControllingInterests": "nci_equity", "NonControllingInterestsIFRS": "nci_equity",
}
INTEREST_BEARING = re.compile(r"(ShortTermLoansPayable|ShortTermBorrowings|LongTermLoansPayable|CurrentPortionOfLongTermLoansPayable|"
                              r"CommercialPapers|BondsPayable|CurrentPortionOfBonds|ConvertibleBond|Borrowings|BondsAndBorrowings|"
                              r"LeaseObligations|LeaseLiabilities|InterestBearingLiabilities)")
LEASE = re.compile(r"Lease(Obligations|Liabilities)")

# ------------------------------------------------------------------ CF
CF_MAP = {
    "NetCashProvidedByUsedInOperatingActivities": "cfo", "NetCashProvidedByUsedInInvestmentActivities": "cfi",
    "NetCashProvidedByUsedInFinancingActivities": "cff",
    "EffectOfExchangeRateChangeOnCashAndCashEquivalents": "fx",
    "NetCashProvidedByUsedInOperatingActivitiesIFRS": "cfo", "NetCashProvidedByUsedInInvestingActivitiesIFRS": "cfi",
    "NetCashProvidedByUsedInFinancingActivitiesIFRS": "cff",
    "EffectOfExchangeRateChangesOnCashAndCashEquivalentsIFRS": "fx",
}
CF_NET_CHANGE = re.compile(r"NetIncreaseDecreaseInCashAndCashEquivalents")
CF_SUBTOTAL = re.compile(r"^Subtotal(OpeCF|OpeCFIFRS)$")
CF_CASH = re.compile(r"^CashAndCashEquivalents(IFRS)?$")
CAPEX = re.compile(r"^(PurchaseOfPropertyPlantAndEquipment|PurchaseOfIntangibleAssets)(InvCF|InvCFIFRS|IFRS)?$")


def local(name: str) -> str:
    return name.split(":")[-1]


SCOPE_MEMBERS = {"ConsolidatedMember", "NonConsolidatedMember"}


def plain_period(p) -> bool:
    """次元メンバーがない、または連結・非連結の区別だけのコンテキスト（非連結の短信は NonConsolidatedMember が付く）。"""
    return all(v.split(":")[-1] in SCOPE_MEMBERS for v in (p.get("members") or {}).values())


def pick_contexts(facts, kind):
    """当期・前期のコンテキストを期間から判定する（連結・非連結以外の次元メンバー付きは除く）。
    次元なしと連結・非連結メンバー付きの両方がある場合は、金額の事実が多い方を使う。"""
    per, scoped = {}, {}
    n_per = n_scoped = 0
    for f in facts:
        p = f["period"]
        if not plain_period(p):
            continue
        dst = scoped if p.get("members") else per
        hit = False
        if kind == "duration" and p.get("start"):
            dst[f["context"]] = (p["start"], p["end"])
            hit = True
        if kind == "instant" and p.get("instant"):
            dst[f["context"]] = (p["instant"],)
            hit = True
        if hit and f.get("unit") == "JPY":
            if p.get("members"):
                n_scoped += 1
            else:
                n_per += 1
    return per if n_per >= n_scoped and per else (scoped or per)


def build_pl(facts, unit_hint):
    durs = pick_contexts(facts, "duration")
    if not durs:
        return None, None
    cur = max(durs, key=lambda c: (durs[c][1], durs[c][0]))
    cur_start, cur_end = durs[cur]
    prior = None
    for c, (s, e) in durs.items():
        if e < cur_end and (prior is None or e > durs[prior][1]):
            prior = c
    rows = {}
    for f in facts:
        if f["context"] not in (cur, prior) or f["unit"] != "JPY":
            continue
        n = local(f["name"])
        r = rows.setdefault(n, {"name": n, "label": f["label"], "order": f["order"], "value": None, "prior": None, "nil": []})
        r["order"] = min(r["order"], f["order"])
        fld = "value" if f["context"] == cur else "prior"
        r[fld] = f["value"]
        if f["nil"]:
            r["nil"].append(fld)
    lines, parent_stack = [], []
    for r in sorted(rows.values(), key=lambda r: r["order"]):
        n = r["name"]
        ln = {"label": r["label"], "value": _num(r["value"]), "prior": _num(r["prior"]), "src": "S1", "xbrl": n}
        if r["nil"]:
            ln["nil_fields"] = r["nil"]   # 決算短信で「－」（計上なし）と表示された欄
        if INFO_RE.search(n) and n not in PL_MAP:
            ln["kind"] = "info"
        elif n in PL_MAP:
            ln["key"], ln["kind"] = PL_MAP[n]
        else:
            suf = next((s for s in PL_CHILD_SUFFIX if n.endswith(s)), None)
            if suf:
                ln["kind"], ln["parent"] = "detail", PL_CHILD_SUFFIX[suf]
            elif n in PL_TAX_CHILDREN:
                ln["kind"], ln["parent"] = "detail", "income_taxes"
            else:
                ln["kind"] = "unknown"
        lines.append(ln)
    # IFRS では「当期利益」の後に「親会社の所有者」「非支配持分」が内訳として並ぶ → net_income の内訳にする
    ni = next((x for x in lines if x.get("key") == "net_income"), None)
    if ni is not None:
        idx = lines.index(ni)
        for x in lines[idx + 1:]:
            if x.get("xbrl") in IFRS_ATTRIB:
                x["parent"] = "net_income"
                if not x["label"].endswith(("利益", "損失", "損益")):
                    x["label"] = f"{x['label']}に帰属する{re.sub(r'（.*?）', '', ni['label'])}"
    # 内訳の kind を親に合わせる（validate では parent 付きは連鎖から除外される）
    for ln in lines:
        if ln.get("kind") == "detail" and ln.get("parent") in set(PL_CHILD_SUFFIX.values()) | {"income_taxes"}:
            ln["kind"] = "income" if ln["parent"] in ("non_op_income", "extraordinary_income") else "expense"
    return lines, {"current": durs[cur], "prior": durs.get(prior)}


ACCUM_RE = re.compile(r"^Accumulated(Depreciation|Impairment|Amortization)")


def build_bs(facts, ifrs=False):
    inst = pick_contexts(facts, "instant")
    if not inst:
        return None
    dates = sorted({v[0] for v in inst.values()})
    used = {}
    for f in facts:
        if f["context"] in inst and f["unit"] == "JPY":
            used.setdefault(inst[f["context"]][0], 0)
            used[inst[f["context"]][0]] += 1
    dates = sorted(d for d in used if used[d] > 5)
    cur_d, prior_d = dates[-1], (dates[-2] if len(dates) > 1 else None)
    rows = {}
    for f in facts:
        if f["context"] not in inst or f["unit"] != "JPY":
            continue
        d_ = inst[f["context"]][0]
        if d_ not in (cur_d, prior_d):
            continue
        n = local(f["name"])
        r = rows.setdefault(n, {"name": n, "label": f["label"], "order": f["order"], "value": None, "prior": None})
        r["order"] = min(r["order"], f["order"])
        r["value" if d_ == cur_d else "prior"] = f["value"]
    ordered = sorted(rows.values(), key=lambda r: r["order"])
    out = {"date": cur_d, "prior_date": prior_d, "assets": [], "liabilities": [], "equity": [],
           "group_totals": {}, "totals": {}}
    pending = []   # グループ合計が出るまで溜める行
    sub_pending = []

    def flush_group(total_key, total_label_row):
        side, gname = GROUP_OF_TOTAL[total_key]
        if ifrs:
            gname = {"固定資産": "非流動資産", "固定負債": "非流動負債", "純資産": "資本"}.get(gname, gname)
        for ln in pending:
            ln["group"] = gname
            out[side].append(ln)
        out["group_totals"][gname] = {"value": total_label_row["value"], "prior": total_label_row["prior"]}
        pending.clear()

    for r in ordered:
        n = r["name"]
        ln = {"label": r["label"], "value": _num(r["value"]), "prior": _num(r["prior"]), "src": "S1", "xbrl": n}
        if n in BS_KEYS:
            ln["key"] = BS_KEYS[n]
        if INTEREST_BEARING.search(n):
            ln["interest_bearing"] = True
            if LEASE.search(n):
                ln["lease"] = True
        if n in BS_TOTALS:
            tk = BS_TOTALS[n]
            out["totals"][tk] = {"value": ln["value"], "prior": ln["prior"], "label": r["label"]}
            if tk in GROUP_OF_TOTAL:
                # 直前のサブグループに属していない行はトップレベル
                pending.extend(sub_pending)
                sub_pending.clear()
                flush_group(tk, ln)
            continue
        if n in BS_SUBGROUPS:
            ln["key"] = ln.get("key") or BS_SUBGROUPS[n]
            for c in sub_pending:
                c["parent"] = ln["key"]
            pending.extend(sub_pending)
            sub_pending.clear()
            pending.append(ln)
            continue
        sub_pending.append(ln)
    pending.extend(sub_pending)
    if pending:   # グループ合計がない行（IFRS の資本の内訳など）
        for ln in pending:
            ln["group"] = ln.get("group") or "未分類"
            out["equity"].append(ln)
    # サブグループの子が「直前のサブグループ合計の後ろ」に並ぶ様式（親が先・子が後）への対応はしない。
    # 親子関係の誤りは validate の合計照合で検出される。
    # 親子の付け方を補正: 子が親より後ろに来ていた場合（親が先頭行）
    for side in ("assets", "liabilities", "equity"):
        for ln in out[side]:
            if ln.get("parent") and not any(x.get("key") == ln["parent"] for x in out[side]):
                ln.pop("parent")
    # 間接控除方式（取得原価・減価償却累計額・純額の3行）：取得原価と累計額を純額の内訳にする（二重計上を防ぐ）
    for side in ("assets",):
        rows_ = out[side]
        n_net = 0
        for i, ln in enumerate(rows_):
            if not (ln["label"].endswith("（純額）") or "Net" in ln["xbrl"]):
                continue
            k = i - 1
            while k >= 0 and ACCUM_RE.match(rows_[k]["xbrl"]) and rows_[k].get("parent") == ln.get("parent"):
                k -= 1
            if k < i - 1 and k >= 0 and rows_[k].get("parent") == ln.get("parent") and not ACCUM_RE.match(rows_[k]["xbrl"]):
                n_net += 1
                ln["key"] = ln.get("key") or f"{ln.get('parent') or 'asset'}_net{n_net}"
                for j in range(k, i):
                    rows_[j]["parent"] = ln["key"]
    # 同じ名前の科目が別グループにある場合（社債及び借入金など）はグループ名を付けて区別する
    from collections import Counter
    cnt = Counter(ln["label"] for side in ("assets", "liabilities", "equity") for ln in out[side] if not ln.get("parent"))
    for side in ("assets", "liabilities", "equity"):
        for ln in out[side]:
            if cnt[ln["label"]] > 1 and not ln.get("parent") and ln.get("group"):
                short = {"流動資産": "流動", "流動負債": "流動", "固定資産": "固定", "固定負債": "固定",
                         "非流動資産": "非流動", "非流動負債": "非流動"}.get(ln["group"], ln["group"])
                ln["label"] = f"{ln['label']}（{short}）"
    # トップレベル行の並びを保ったまま、子は親の直後に置く（孫は子の直後）
    for side in ("assets", "liabilities", "equity"):
        rows_ = out[side]

        def place(t, res):
            res.append(t)
            for c in rows_:
                if c.get("parent") and t.get("key") and c["parent"] == t["key"]:
                    place(c, res)
        res: list = []
        for t in [ln for ln in rows_ if not ln.get("parent")]:
            place(t, res)
        out[side] = res
    return out


def build_cf(facts, pl_periods):
    durs = pick_contexts(facts, "duration")
    inst = pick_contexts(facts, "instant")
    if not durs:
        return None
    cur = max(durs, key=lambda c: (durs[c][1], durs[c][0]))
    prior = None
    for c, (s, e) in durs.items():
        if e < durs[cur][1] and (prior is None or e > durs[prior][1]):
            prior = c
    rows, cash = {}, {}
    for f in facts:
        n = local(f["name"])
        if f["unit"] != "JPY" or not plain_period(f["period"]):
            continue
        if CF_CASH.match(n) and f["context"] in inst:
            cash[inst[f["context"]][0]] = _num(f["value"])
            continue
        if f["context"] not in (cur, prior):
            continue
        r = rows.setdefault(n, {"name": n, "label": f["label"], "order": f["order"], "value": None, "prior": None, "nil": []})
        r["order"] = min(r["order"], f["order"])
        fld = "value" if f["context"] == cur else "prior"
        r[fld] = f["value"]
        if f["nil"]:
            r["nil"].append(fld)
    lines, children, capex, capex_p = [], [], 0, 0
    has_capex = False
    net_change = None
    for r in sorted(rows.values(), key=lambda r: r["order"]):
        n = r["name"]
        ln = {"label": r["label"], "value": _num(r["value"]), "prior": _num(r["prior"]), "src": "S1", "xbrl": n}
        if r["nil"]:
            ln["nil_fields"] = r["nil"]
        if CF_SUBTOTAL.match(n):
            ln["kind"] = "subtotal"   # 営業CFの「小計」。内訳の合計照合から除く
        if CAPEX.match(n):
            has_capex = True
            capex += abs(ln["value"] or 0)
            capex_p += abs(ln["prior"] or 0)
        if n in CF_MAP:
            ln["key"] = CF_MAP[n]
            if ln["key"] in ("cfo", "cfi", "cff"):
                for c in children:
                    c["parent"] = ln["key"]
                lines.extend(children)
                children = []
            lines.append(ln)
        elif CF_NET_CHANGE.search(n):
            net_change = ln
        elif "IncreaseDecreaseInCashAndCashEquivalentsResultingFromChangeInScopeOfConsolidation" in n or \
                "IncreaseInCashAndCashEquivalentsFromNewlyConsolidatedSubsidiary" in n or "Merger" in n:
            ln["key"] = ln.get("key") or "scope_change"
            lines.append(ln)
        else:
            children.append(ln)
    # 最後の活動区分より後ろに残った行（売却目的で保有する資産への振替に伴う現金の増減など）は、
    # 現金及び現金同等物の増減に直接効く行なので、期首→期末の照合に入る独立の行として残す
    leftover = []
    for ln in children:
        if "CashAndCashEquivalents" in ln["xbrl"]:
            ln["key"] = "other_cash_change"
            lines.append(ln)
        else:
            leftover.append(ln["label"])
    # 子行は親の前に並ぶので、表示順は「親→子」に並べ替える
    ordered = []
    for ln in [x for x in lines if not x.get("parent")]:
        ordered.append(ln)
        ordered.extend(x for x in lines if x.get("parent") == ln.get("key"))
    s, e = durs[cur]
    ps, pe = durs[prior] if prior else (None, None)

    def day_before(iso):
        return (date.fromisoformat(iso) - timedelta(days=1)).isoformat() if iso else None
    out = {"disclosed": True, "cash_definition": "現金及び現金同等物", "lines": ordered,
           "begin": {"label": "現金及び現金同等物の期首残高", "value": cash.get(day_before(s)),
                     "prior": cash.get(day_before(ps)), "src": "S1"},
           "end": {"label": "現金及び現金同等物の期末残高", "value": cash.get(e), "prior": cash.get(pe), "src": "S1"}}
    if net_change:
        out["net_change"] = {"label": net_change["label"], "value": net_change["value"], "prior": net_change["prior"]}
    if leftover:
        out["unassigned_lines"] = leftover   # どの区分にも属さず、照合にも入れなかった行（レビューで確認する）
    if has_capex:
        out["capex"] = {"label": "有形・無形固定資産の取得による支出", "value": capex, "prior": capex_p or None, "src": "S1"}
    return out


def _num(v):
    if v is None:
        return None
    from decimal import Decimal
    d = Decimal(v)
    return int(d) if d == d.to_integral_value() else float(d)


def norm_code(c):
    """TDnet の5桁コード（末尾がチェック用の0）を4桁の証券コードにする。"""
    c = (c or "").strip()
    return c[:4] if len(c) == 5 and c.endswith("0") else c


def summary_meta(zip_path):
    import zipfile
    zf = zipfile.ZipFile(zip_path)
    meta = {}
    for n in zf.namelist():
        if "/Summary/" in n and n.endswith("-ixbrl.htm"):
            doc = zf.read(n).decode("utf-8", "replace")
            # 入れ子の ix:nonNumeric があるため、開始タグごとに次の終了タグまでを内容とする（内側の要素も拾う）
            for m in re.finditer(r"<ix:nonNumeric\b([^>]*)>", doc):
                nm = re.search(r'name="tse-ed-t:([^"]+)"', m.group(1))
                if not nm:
                    continue
                end = doc.find("</ix:nonNumeric>", m.end())
                inner = doc[m.end():end]
                if "<ix:nonNumeric" in inner:
                    inner = inner[:inner.find("<ix:nonNumeric")]
                txt = re.sub(r"<[^>]+>", "", inner).strip()
                if txt:
                    meta.setdefault(nm.group(1), txt)
            if "SecuritiesCode" not in meta:
                fm = re.search(r"-(\d{3}[0-9A-Z]0)-\d{8}", n)   # ファイル名の5桁コード
                if fm:
                    meta["SecuritiesCode"] = fm.group(1)
    return meta


def summary_numbers(res, nonconsolidated=False):
    """短信サマリーの予想・自己資本など。nonconsolidated=True（非連結の短信）なら非連結の予想を使う。"""
    out = {"forecast": {}, "owners_equity": {}}
    for f in res["facts"]:
        if f["statement"] != "summary" or f["value"] is None:
            continue
        n, mem = local(f["name"]), set(v.split(":")[-1] for v in f["period"].get("members", {}).values())
        ctx = f["context"]
        scope_ok = ("NonConsolidatedMember" in mem) if nonconsolidated else ("NonConsolidatedMember" not in mem)
        if "ForecastMember" in mem and scope_ok:
            if not ("CurrentYear" in ctx or "NextYear" in ctx):
                continue
            if "Q2" in ctx or "SecondQuarter" in ctx or "Interim" in ctx:
                continue
            for base, key in ((r"(NetSales|Sales|Revenue|OperatingRevenues?|NetRevenue)(IFRS|US)?", "revenue"),
                              (r"Operating(Income|Profit)(IFRS|US)?", "operating_income"),
                              (r"OrdinaryIncome", "ordinary_income"),
                              (r"(ProfitBeforeTax|IncomeBeforeIncomeTaxes)(IFRS|US)?", "pretax_income"),
                              (r"(ProfitAttributableToOwnersOfParent|NetIncomeAttributableToOwnersOfParent)(IFRS|US)?", "net_income_parent"),
                              (r"NetIncome|Profit(IFRS|US)?", "net_income"),   # 非連結の当期純利益
                              (r"(NetIncomePerShare|BasicEarningsPerShare)(IFRS|US)?", "eps")):
                if re.fullmatch(base, n):
                    out["forecast"].setdefault(key, f["value"])
                    out["forecast"].setdefault("_period_end", f["period"].get("end"))
                elif re.fullmatch(r"Change(In)?(?:" + base + ")", n):
                    out["forecast"].setdefault(f"{key}_change_pct", f["value"])
        if n == "DividendPerShare" and "ForecastMember" in mem and "AnnualMember" in mem:
            out["forecast"].setdefault("dividend", f["value"])
        # 連結の短信には「（参考）個別業績」の自己資本・自己資本比率（NonConsolidatedMember）も載る。
        # 予想と同じく連結・非連結を区別しないと、後に出てくる個別の値で上書きされる。
        if not scope_ok or "ForecastMember" in mem:
            continue
        if re.match(r"^(OwnersEquity|EquityAttributableToOwnersOfParent)(IFRS|US)?$", n) and f["period"].get("instant"):
            out["owners_equity"][f["period"]["instant"]] = f["value"]
        if n == "CapitalAdequacyRatio" or re.match(r"^RatioOfOwnersEquityToGrossAssets", n):
            out.setdefault("equity_ratio_disclosed", {})[f["period"].get("instant")] = f["value"]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zip")
    ap.add_argument("--pdf-url", required=True)
    ap.add_argument("--xbrl-url")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    res = extract(a.zip)
    meta = summary_meta(a.zip)
    # 財務諸表の表示単位（最も多い scale）。サマリーは百万円、本表は千円という会社があるため、
    # 円換算値（value_yen）から本表の単位に揃える。1株当たり金額・比率（JPY 以外）はそのまま。
    from collections import Counter
    from decimal import Decimal
    sc = Counter(f["scale"] for f in res["facts"]
                 if f["statement"] not in ("summary", None) and f["unit"] == "JPY" and f["value"] is not None)
    scale = sc.most_common(1)[0][0] if sc else 6
    stmt_unit = {6: "百万円", 3: "千円", 0: "円"}.get(scale)
    if stmt_unit is None:
        print(json.dumps({"ok": False, "error": f"未対応の表示単位（scale={scale}）"}, ensure_ascii=False))
        sys.exit(1)
    factor = Decimal(10) ** scale
    for f in res["facts"]:
        if f["unit"] == "JPY" and f.get("value_yen") is not None:
            f["value"] = str((Decimal(f["value_yen"]) / factor).normalize())
    facts_by = {}
    for f in res["facts"]:
        facts_by.setdefault(f["statement"], []).append(f)
    meta_doc = unicodedata.normalize("NFKC", meta.get("DocumentName", ""))
    ifrs = "IFRS" in meta_doc
    pl_src = facts_by.get("pl") or facts_by.get("ci", [])   # IFRS の1計算書方式は PL が包括利益計算書に入っている
    pl_lines, per = build_pl(pl_src, None)
    if not pl_lines or not per:
        print(json.dumps({"ok": False, "error": "XBRL から損益計算書の行を取得できない。PDF から手入力すること",
                          "statements": sorted(facts_by)}, ensure_ascii=False))
        sys.exit(1)
    bs = build_bs(facts_by.get("bs", []), ifrs=ifrs)
    cf = build_cf(facts_by.get("cf", []), per) if facts_by.get("cf") else None
    docname = meta.get("DocumentName", "")
    std = "IFRS" if ifrs else ("US-GAAP" if "米国基準" in meta_doc else "J-GAAP")
    scope = "単体" if "非連結" in docname else "連結"
    sm = summary_numbers(res, nonconsolidated=(scope == "単体"))
    qp = meta.get("QuarterlyPeriod")
    qn = next((f["value"] for f in res["facts"] if local(f["name"]) == "QuarterlyPeriod"), None)
    ptype = "FY" if not qn else f"Q{int(float(qn))}"
    start, end = per["current"] if per else (None, None)
    months = None
    if start and end:
        sd, ed = date.fromisoformat(start), date.fromisoformat(end)
        months = (ed.year - sd.year) * 12 + ed.month - sd.month + 1
    fy_end = date.fromisoformat(end) if end else None
    fy_label = None
    if fy_end:
        fy_year_end = date.fromisoformat(start).replace(year=date.fromisoformat(start).year + 1) - timedelta(days=1)
        fy_label = f"{fy_year_end.year}年{fy_year_end.month}月期"
    name = meta.get("CompanyName", "")
    unit_note = meta.get("NoteToFractionProcessingMethod", "").strip("()（）")
    filing = meta.get("FilingDate", "")
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", filing)
    filing_iso = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None
    fc = sm["forecast"]
    forecast = None
    if fc:
        pe = fc.pop("_period_end", None)
        forecast = {"period": (f"{date.fromisoformat(pe).year}年{date.fromisoformat(pe).month}月期" if pe else None),
                    "as_of": filing_iso, "src": "S1"}
        forecast.update({k: _num(v) for k, v in fc.items()})
        forecast["revised"] = meta.get("CorrectionOfConsolidatedFinancialForecastInThisQuarter") == "有"
    data = {
        "schema_version": 1,
        "sample": False,
        "draft": True,
        "company": {"name": name, "code": norm_code(meta.get("SecuritiesCode")), "industry": None,
                    "fiscal_year_end_month": fy_end.month if ptype == "FY" and fy_end else None,
                    "url": meta.get("URL")},
        "period": {"label": f"{fy_label}_{ {'FY': '本決算', 'Q1': '第1四半期', 'Q2': '第2四半期', 'Q3': '第3四半期'}[ptype]}",
                   "fiscal_year": fy_label, "type": ptype, "start": start, "end": end, "months": months,
                   "is_cumulative": True, "published": filing_iso},
        "basis": {"standard": std, "scope": scope, "unit": stmt_unit,
                  "rounding": (unit_note if (not unit_note or unit_note.startswith(stmt_unit))
                               else f"財務諸表は{stmt_unit}単位（サマリーは{unit_note}）")},
        "sources": [{"id": "S1", "title": f"{fy_label} {docname}", "url": a.pdf_url, "published": filing_iso,
                     "kind": "決算短信", "xbrl_url": a.xbrl_url}],
        "pl": {"lines": pl_lines or [], "revenue_key": "revenue", "split_after": "operating_income",
               # 非連結など「親会社株主に帰属する」行がない会社は当期純利益の行を使う
               "net_income_key": "net_income_parent" if any(ln.get("key") == "net_income_parent" for ln in pl_lines or [])
               else "net_income"},
        "bs": bs or {},
        "cf": cf if cf else {"disclosed": False, "reason_if_not": f"{docname}の添付資料にキャッシュ・フロー計算書が含まれていない（XBRL にも CF の要素なし）"},
        "segments": [],
        "forecast": forecast,
        "extra": [],
        "notes": [],
        "review_todo": [
            "company.industry（東証33業種）を記入",
            "各行の src に頁番号を追記（例: 'S1 p.7'）",
            "kind=unknown の行を分類",
            "BS: 重要科目に highlight=true、有利子負債の判定を確認",
            "セグメント情報を追加",
            "完了したら draft=false",
        ],
    }
    if bs and sm["owners_equity"]:
        oe = sm["owners_equity"]
        data["bs"]["owners_equity"] = {"label": "自己資本", "value": _num(oe.get(bs["date"])),
                                       "prior": _num(oe.get(bs["prior_date"])), "src": "S1",
                                       "definition": "決算短信サマリーの自己資本（純資産 − 新株予約権 − 非支配株主持分）"
                                       if std == "J-GAAP" else "親会社の所有者に帰属する持分"}
        # サマリー（百万円）より本表（千円など）の単位が細かい場合、サマリーの値は端数が切られている。
        # 日本基準では BS から「純資産 − 新株予約権 − 非支配株主持分」を計算し、サマリーの値と1単位以内で一致すればそれを使う。
        na = (bs.get("totals") or {}).get("net_assets") or {}
        if std == "J-GAAP" and stmt_unit != "百万円" and na:
            tol = Decimal(10) ** (6 - scale)
            ok, calc_v = True, {}
            for fld, dt in (("value", bs["date"]), ("prior", bs["prior_date"])):
                if na.get(fld) is None:
                    continue
                minus = sum((Decimal(str(ln[fld])) for ln in bs.get("equity", [])
                             if not ln.get("parent") and ln.get(fld) is not None
                             and re.search(r"新株予約権|非支配株主持分", ln["label"])), Decimal(0))
                cv_ = Decimal(str(na[fld])) - minus
                sv = oe.get(dt)
                if sv is not None and abs(cv_ - Decimal(str(sv))) >= tol:
                    ok = False
                calc_v[fld] = cv_
            if ok and calc_v:
                data["bs"]["owners_equity"].update({k: _num(v) for k, v in calc_v.items()})
                data["bs"]["owners_equity"]["definition"] = (
                    f"純資産 − 新株予約権 − 非支配株主持分（BS の{stmt_unit}単位の値から計算。決算短信サマリーの自己資本（百万円単位）と一致）")
        # IFRS：本表の「親会社の所有者に帰属する持分合計」（千円など）を使う。サマリー（百万円）と1単位以内で一致すること
        if std != "J-GAAP" and stmt_unit != "百万円":
            tol = Decimal(10) ** (6 - scale)
            ln_oe = next((ln for ln in bs.get("equity", []) if ln.get("key") == "owners_equity_ifrs"), None)
            if ln_oe:
                pairs = [(fld, dt) for fld, dt in (("value", bs["date"]), ("prior", bs["prior_date"]))
                         if ln_oe.get(fld) is not None]
                if pairs and all(oe.get(dt) is None or abs(Decimal(str(ln_oe[fld])) - Decimal(str(oe[dt]))) < tol
                                 for fld, dt in pairs):
                    data["bs"]["owners_equity"].update({fld: ln_oe[fld] for fld, _ in pairs})
                    data["bs"]["owners_equity"]["definition"] = (
                        f"親会社の所有者に帰属する持分（BS の{stmt_unit}単位の値。決算短信サマリー（百万円単位）と一致）")
    if sm.get("equity_ratio_disclosed"):
        data["extra"].append({"label": "自己資本比率（会社開示）", "values": sm["equity_ratio_disclosed"], "unit": "%", "src": "S1"})
    # 片方の期だけ XBRL の値がない行（nil でもない）。PDF で「－」なら nil_fields に入れ、空欄なら null のままにする。
    one_sided = []
    all_lines = [ln for sec in ([data["pl"]["lines"]] + [data["bs"].get(k) or [] for k in ("assets", "liabilities", "equity")]
                                + [data["cf"].get("lines") or []]) for ln in sec]
    no_prior_at_all = bool(all_lines) and all(ln.get("prior") is None and "prior" not in (ln.get("nil_fields") or [])
                                              for ln in all_lines)
    for ln in all_lines:
        nil = set(ln.get("nil_fields") or [])
        for fld, other in (("value", "prior"), ("prior", "value")):
            if no_prior_at_all and fld == "prior":
                continue
            if ln.get(fld) is None and fld not in nil and ln.get(other) is not None:
                one_sided.append(f"{ln['label']}（{'当期' if fld == 'value' else '前期'}）")
    if no_prior_at_all:
        data["review_todo"].insert(0, "前期・前年同期の値が XBRL に1つもない（初めての連結決算、決算期変更などで比較情報を開示していない）。"
                                   "prior は null のままにし、比較できない理由を notes に書く。会社が説明資料などで参考値を示していれば、"
                                   "出典と「参考値」であることを付けて extra に入れる")
    if one_sided:
        data["review_todo"].insert(0, "XBRL に値がない欄: " + "、".join(one_sided)
                                   + "。PDF で「－」と表示されていれば nil_fields に追加し、notes に根拠の頁を書く")
    if (data["cf"] or {}).get("unassigned_lines"):
        data["review_todo"].insert(0, "CF のどの区分にも属さない行: " + "、".join(data["cf"]["unassigned_lines"])
                                   + "。PDF の CF 計算書で位置を確認し、期首→期末の照合に必要なら独立の行として追加する")
    if any(ln.get("key") == "other_cash_change" for ln in (data["cf"] or {}).get("lines") or []):
        data["review_todo"].insert(0, "CF に活動区分外の現金増減の行（other_cash_change）がある。PDF で表示位置と符号を確認する")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    unknown = [ln["label"] for ln in data["pl"]["lines"] if ln.get("kind") == "unknown"]
    print(json.dumps({"ok": True, "out": a.out, "period": data["period"], "basis": data["basis"],
                      "pl_lines": len(data["pl"]["lines"]), "bs_lines": sum(len(bs.get(s, [])) for s in ("assets", "liabilities", "equity")) if bs else 0,
                      "cf_disclosed": bool(cf), "unknown_pl_lines": unknown}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
