#!/usr/bin/env python3
"""financial-report-analyzer の自動テスト（標準ライブラリのみ）。

- 一時ディレクトリに作ったテスト用 Vault だけを使う。利用者の Vault と設定ファイルは変更しない
  （FRA_CONFIG を一時ファイルに向ける）。
- サンプルデータ（tests/fixtures/sample_*.json）は架空の数値で、sample=true が付いている。
- 実データのテスト（XBRL 解析）はキャッシュに TDnet の XBRL がある場合だけ実行する。
- --live を付けると TDnet への接続テストも行う。

使い方:
  python3 -I tests/run_tests.py [--live] [--only <テスト名の一部>] [--keep]
出力: 1行目以降に各テストの結果（JSON Lines）、最後に集計（JSON）。失敗があれば exit 1。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIX = ROOT / "tests" / "fixtures"
PY = [sys.executable, "-I"]
TMP = Path(tempfile.mkdtemp(prefix="fra-test-"))
VAULT = TMP / "vault"
ENV = dict(os.environ, FRA_CONFIG=str(TMP / "config.json"))
NINTENDO_ZIP = Path.home() / ".cache/financial-report-analyzer/downloads/7974/2027年3月期_第2四半期/081220261027557139.zip"
TSUBAKI_ZIP = Path.home() / ".cache/financial-report-analyzer/downloads/6464/2026年12月期_第3四半期/081220261113579718.zip"
TDK_ZIP = Path.home() / ".cache/financial-report-analyzer/downloads/6762/2027年3月期_第2四半期/081220261029560105.zip"
LOGIZARD_ZIP = Path.home() / ".cache/financial-report-analyzer/downloads/4391/2027年6月期_第1四半期/081220261111574017.zip"
SPIDER_ZIP = Path.home() / ".cache/financial-report-analyzer/downloads/4192/2026年12月期_第3四半期/081220261111574195.zip"
EWELL_ZIP = Path.home() / ".cache/financial-report-analyzer/downloads/5038/2026年12月期_第3四半期/081220261110571892.zip"
HIKARI_ZIP = Path.home() / ".cache/financial-report-analyzer/downloads/9435/2027年3月期_第2四半期/081220261104564190.zip"
BROADLEAF_ZIP = Path.home() / ".cache/financial-report-analyzer/downloads/3673/2026年12月期_第3四半期/081220261111573473.zip"
KANAMIC_ZIP = Path.home() / ".cache/financial-report-analyzer/downloads/3939/2026年9月期_本決算/081220261113579807.zip"
CYBERLINKS_ZIP = Path.home() / ".cache/financial-report-analyzer/downloads/3683/2026年12月期_第3四半期/081220261111572905.zip"


def run(script, *args, ok_codes=(0,)):
    r = subprocess.run(PY + [str(SCRIPTS / script), *map(str, args)], capture_output=True, text=True, env=ENV,
                       timeout=300)
    if r.returncode not in ok_codes:
        raise AssertionError(f"{script} {' '.join(map(str, args))} → exit {r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return r


def js(r):
    return json.loads(r.stdout)


def setup_env():
    (VAULT / ".obsidian").mkdir(parents=True)
    cfg = {"vault_path": str(VAULT), "root_folder": "決算分析", "cache_dir": str(TMP / "cache"), "embed_width": 760,
           "display_unit": "auto", "debt_includes_lease": False}
    (TMP / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# ---------------------------------------------------------------- 1. スキルの定義

@test
def skill_definition():
    s = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", s, re.S)
    assert m, "SKILL.md に YAML frontmatter がない"
    fm = m.group(1)
    assert re.search(r"^name: financial-report-analyzer$", fm, re.M), "name が不正"
    desc = re.search(r"^description: (.+)$", fm, re.M).group(1)
    wtu = re.search(r"^when_to_use: (.+)$", fm, re.M).group(1)
    assert len(desc) + len(wtu) <= 1536, f"description+when_to_use が長すぎる（{len(desc) + len(wtu)}字）"
    assert len(s.splitlines()) < 500, "SKILL.md は500行未満にする"
    for ref in re.findall(r"\]\((references/[^)]+)\)", s):
        assert (ROOT / ref).exists(), f"参照先がない: {ref}"
    for sc in set(re.findall(r"\$S/(\w+\.py)", s)):
        assert (SCRIPTS / sc).exists(), f"スクリプトがない: {sc}"
    return {"description_chars": len(desc) + len(wtu), "lines": len(s.splitlines())}


@test
def scripts_compile():
    for p in SCRIPTS.glob("*.py"):
        subprocess.run([sys.executable, "-m", "py_compile", str(p)], check=True, capture_output=True)
    return {"files": len(list(SCRIPTS.glob("*.py")))}


# ---------------------------------------------------------------- 2. 取得・解析（実データ）

@test
def xbrl_real_nintendo():
    if not NINTENDO_ZIP.exists():
        return {"skipped": "キャッシュに任天堂の XBRL がない（TDnet の公開期間外の場合は IR サイトの資料でテストする）"}
    out = TMP / "n.json"
    run("xbrl_to_data.py", NINTENDO_ZIP, "--pdf-url", "https://www.release.tdnet.info/inbs/140120261027557139.pdf", "--out", out)
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["company"]["code"] == "7974", d["company"]["code"]
    assert d["period"]["type"] == "Q2" and d["period"]["start"] == "2026-04-01" and d["period"]["end"] == "2026-09-30"
    assert d["basis"] == {"standard": "J-GAAP", "scope": "連結", "unit": "百万円", "rounding": "百万円未満切捨て"}
    k = {ln.get("key"): ln for ln in d["pl"]["lines"]}
    assert k["revenue"]["value"] == 966041 and k["revenue"]["prior"] == 1099565
    assert k["operating_income"]["value"] == 216500
    assert k["net_income_parent"]["value"] == 220917
    assert d["bs"]["totals"]["assets"]["value"] == 3936480
    assert d["cf"]["disclosed"] is False, "中間期短信に CF はない"
    assert not [ln for ln in d["pl"]["lines"] if ln["kind"] == "unknown"]
    v = js(run("fin.py", "validate", out))
    assert v["ok"], v["errors"]
    fx = next(ln for ln in d["pl"]["lines"] if ln["label"] == "為替差益")
    assert fx["value"] is None and fx.get("nil_fields") == ["value"], "「－」表示を nil として記録する"
    return {"rounding_diffs": len(v["rounding_diffs"])}


@test
def xbrl_real_ifrs_loss():
    if not TSUBAKI_ZIP.exists():
        return {"skipped": "キャッシュにツバキ・ナカシマの XBRL がない"}
    out = TMP / "t.json"
    run("xbrl_to_data.py", TSUBAKI_ZIP, "--pdf-url", "https://www.release.tdnet.info/inbs/140120261113579718.pdf", "--out", out)
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["basis"]["standard"] == "IFRS", "全角「ＩＦＲＳ」を IFRS と判定する"
    assert d["company"]["code"] == "6464" and d["period"]["type"] == "Q3" and d["period"]["months"] == 9
    k = {ln.get("key"): ln for ln in d["pl"]["lines"]}
    assert k["revenue"]["value"] == 54832 and k["operating_income"]["value"] == -5963
    assert k["net_income_parent"]["parent"] == "net_income", "IFRS の帰属内訳は当期利益の内訳"
    assert not [ln for ln in d["pl"]["lines"] if ln["kind"] == "unknown"]
    assert set(d["bs"]["group_totals"]) == {"流動資産", "非流動資産", "流動負債", "非流動負債", "資本"}
    assert d["bs"]["totals"]["noncurrent_liabilities"]["value"] == 45598, "NonCurrentLabilitiesIFRS（綴り）に対応"
    debt = [ln["label"] for ln in d["bs"]["liabilities"] if ln.get("interest_bearing")]
    assert len(debt) == len(set(debt)) == 2, debt
    sub = [ln for ln in d["cf"]["lines"] if ln.get("kind") == "subtotal"]
    assert sub and sub[0]["parent"] == "cfo", "営業CFの小計を subtotal として扱う"
    fc = d["forecast"]
    assert fc["eps"] == -339.76 and "eps_change_pct" not in fc and fc["pretax_income"] == -12200
    v = js(run("fin.py", "validate", out))
    assert v["ok"], v["errors"]
    r = js(run("diagrams.py", "build", out, "--out-dir", TMP / "fig_ifrs"))
    assert r["ok"] and {f["figure"] for f in r["figures"]} == {"PL", "BS", "CF"}, r
    pl = Path(next(f["drawio"] for f in r["figures"] if f["figure"] == "PL")).read_text(encoding="utf-8")
    assert 'fra_name="営業損失"' in pl, "「営業利益（△損失）」から損失名を作る"
    return {"checks": len(v["checks"])}


@test
def xbrl_real_tdk_nil_and_ifrs_labels():
    """TDK（IFRS）：その他の営業収益・費用の分類、XBRL に当期値がない欄の検出、BS 表の「－」と IFRS の合計名、前回予想の照合。"""
    if not TDK_ZIP.exists():
        return {"skipped": "キャッシュに TDK の XBRL がない"}
    out = TMP / "tdk.json"
    run("xbrl_to_data.py", TDK_ZIP, "--pdf-url", "https://www.release.tdnet.info/inbs/140120261029560105.pdf", "--out", out)
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["company"]["code"] == "6762" and d["basis"]["standard"] == "IFRS"
    kinds = {ln["label"]: ln["kind"] for ln in d["pl"]["lines"]}
    assert kinds["その他の営業収益"] == "income" and kinds["その他の営業費用"] == "expense", kinds
    assert "資本剰余金（当期）" in d["review_todo"][0], "XBRL に当期の値がない欄をレビュー項目に挙げる"
    cs = next(ln for ln in d["bs"]["equity"] if ln["label"] == "資本剰余金")
    assert cs["value"] is None and "nil_fields" not in cs, "nil でない欠落は推測で埋めない"
    cs["nil_fields"] = ["value"]          # PDF で「－」を確認した後の状態
    d["forecast"]["previous"] = {"revenue": 2580000, "operating_income": 295000}
    out.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    v = js(run("fin.py", "validate", out))
    assert v["ok"], v["errors"]
    t = run("fin.py", "tables", out, "--only", "bs").stdout
    assert "| 　資本剰余金 | 273 | － | △273 |" in t, "「－」の欄は未開示ではなく「－」と表示し、増減は 0 として計算する"
    assert "**資本合計**" in t and "**負債及び資本合計**" in t and "純資産合計" not in t, "IFRS の合計名"
    sys.path.insert(0, str(SCRIPTS))
    import check_report
    lv = check_report.labelled_values(d)
    yen = 1_000_000
    assert 2580000 * yen in lv["売上高"]["amt"] and 470000 * yen in lv["売上高"]["amt"], "前回予想と修正幅を照合に使う"
    assert 280000 * yen in lv["親会社の所有者に帰属する当期利益"]["amt"], "IFRS の当期利益の表記"
    return {"checks": len(v["checks"])}


@test
def xbrl_real_hikari_cf_outside_activities():
    """光通信（IFRS）：活動区分の外にある現金増減の行を残す、営業外損益・有利子負債の分類、CF 照合での「－」の扱い。"""
    if not HIKARI_ZIP.exists():
        return {"skipped": "キャッシュに光通信の XBRL がない"}
    out = TMP / "hikari.json"
    run("xbrl_to_data.py", HIKARI_ZIP, "--pdf-url", "https://www.release.tdnet.info/inbs/140120261104564190.pdf", "--out", out)
    d = json.loads(out.read_text(encoding="utf-8"))
    kinds = {ln["label"]: ln["kind"] for ln in d["pl"]["lines"]}
    assert kinds["その他の営業外損益（△は損失）"] == "income", kinds
    debt = [ln["label"] for ln in d["bs"]["liabilities"] if ln.get("interest_bearing")]
    assert debt == ["有利子負債（流動）", "有利子負債（非流動）"], debt
    oc = [ln for ln in d["cf"]["lines"] if ln.get("key") == "other_cash_change"]
    assert len(oc) == 1 and oc[0]["value"] == -717 and not oc[0].get("parent"), "売却目的資産への振替（△717）を照合に入る行として残す"
    assert any("other_cash_change" in t for t in d["review_todo"]), "レビュー項目に挙げる"
    assert oc[0]["prior"] is None and oc[0].get("nil_fields") == ["prior"], "前中間期の「-」は XBRL の nil"
    for ln in d["bs"]["assets"] + d["bs"]["liabilities"]:
        if ln["prior"] is None:
            ln["nil_fields"] = ["prior"]
    out.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    v = js(run("fin.py", "validate", out))
    assert v["ok"] and not v["warnings"], v["warnings"]
    cf_checks = [c for c in v["checks"] if c["check"].startswith("期首残高") and c["where"].startswith("CF(")]
    assert {c["where"] for c in cf_checks} == {"CF(value)", "CF(prior)"}, "「－」を 0 として前期も照合する"
    sys.path.insert(0, str(SCRIPTS))
    import diagrams
    assert diagrams._short_cf(oc[0]["label"]) == "売却目的資産への振替"
    return {"checks": len(v["checks"])}


@test
def xbrl_real_broadleaf_ifrs_thousand_yen_owners_equity():
    """ブロードリーフ（IFRS・千円単位）：自己資本はサマリー（百万円）ではなく BS の千円の値を使う。"""
    if not BROADLEAF_ZIP.exists():
        return {"skipped": "キャッシュにブロードリーフの XBRL がない"}
    out = TMP / "broadleaf.json"
    run("xbrl_to_data.py", BROADLEAF_ZIP, "--pdf-url", "https://www.release.tdnet.info/inbs/140120261111573473.pdf", "--out", out)
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["basis"]["standard"] == "IFRS" and d["basis"]["unit"] == "千円", d["basis"]
    oe = d["bs"]["owners_equity"]
    assert oe["value"] == 25082076 and oe["prior"] == 24271153, oe
    assert "BS の千円単位" in oe["definition"], oe["definition"]
    nci = next(ln for ln in d["bs"]["equity"] if ln.get("key") == "nci_equity")
    assert nci["value"] == -5758, "非支配持分のマイナスをそのまま取得"
    assert d["forecast"]["revenue"] == 22500000, "予想も千円に換算"
    assert "その他の金融資産（流動）（当期）" in d["review_todo"][0], "XBRL に当期の値がない欄をレビュー項目に挙げる"
    v = js(run("fin.py", "validate", out))
    assert v["ok"], v["errors"]
    return {"checks": len(v["checks"])}


@test
def xbrl_real_ewell_nonconsolidated_thousand_yen():
    """eWeLL（非連結・財務諸表は千円単位）：NonConsolidatedMember のコンテキスト、千円単位、非連結の予想と当期純利益、
    自己資本の BS からの計算、BS 図で端数の差を「その他」にしないこと、売上内訳図の表の見出し。"""
    if not EWELL_ZIP.exists():
        return {"skipped": "キャッシュに eWeLL の XBRL がない"}
    out = TMP / "ewell.json"
    run("xbrl_to_data.py", EWELL_ZIP, "--pdf-url", "https://www.release.tdnet.info/inbs/140120261110571892.pdf", "--out", out)
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["basis"]["scope"] == "単体" and d["basis"]["unit"] == "千円", d["basis"]
    k = {ln.get("key"): ln for ln in d["pl"]["lines"]}
    assert k["revenue"]["value"] == 3067567 and k["net_income"]["value"] == 1032500
    assert d["pl"]["net_income_key"] == "net_income", "非連結は当期純利益の行を純利益として使う"
    fc = d["forecast"]
    assert fc["revenue"] == 4277000 and fc["net_income"] == 1329000, "非連結の予想を本表の単位（千円）で取る"
    assert d["bs"]["owners_equity"]["value"] == 3905725, "サマリーの百万円の値ではなく BS の千円の値から自己資本を計算する"
    assert d["cf"]["disclosed"] is False
    assert any("賞与引当金（前期）" in t for t in d["review_todo"])
    for ln in d["bs"]["liabilities"]:
        if ln["label"] == "賞与引当金":
            ln["nil_fields"] = ["prior"]
    d["revenue_breakdown"] = [{"label": "クラウド", "value": 2617000, "src": "S2 p.9"},
                              {"label": "BPaaS", "value": 440000, "src": "S2 p.9"},
                              {"label": "その他", "value": 10567, "src": "S2 p.9"}]
    out.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    v = js(run("fin.py", "validate", out))
    assert v["ok"], v["errors"]
    m = run("fin.py", "tables", out, "--only", "metrics").stdout
    assert "| 収益性 | 純利益率 |" in m and "親会社株主帰属" not in m
    f = run("fin.py", "tables", out, "--only", "forecast").stdout
    assert "| 当期純利益 | 1,329,000 |" in f
    r = js(run("diagrams.py", "build", out, "--out-dir", TMP / "fig_ewell", "--prefix", "E"))
    assert r["ok"], r
    bs = Path(next(x["drawio"] for x in r["figures"] if x["figure"] == "BS")).read_text(encoding="utf-8")
    tiny = [int(v_) for v_ in re.findall(r'fra_name="その他" fra_value="(\d+)"', bs) if int(v_) < 10]
    assert not tiny, f"端数の差（{tiny}千円）を「その他」のブロックにしない"
    seg = Path(next(x["drawio"] for x in r["figures"] if x["figure"] == "SEG")).read_text(encoding="utf-8")
    assert 'value="区分"' in seg and 'value="セグメント"' not in seg, "売上内訳の表の見出しは「区分」"
    return {"unit": d["basis"]["unit"], "checks": len(v["checks"])}


@test
def xbrl_real_spider_jgaap_or_labels_and_breakdown_unit():
    """スパイダープラス（日本基準・千円単位）：「営業利益又は営業損失（△）」の図の表示名、
    百万円単位の売上内訳（revenue_breakdown_unit）、check_report の科目名の照合。"""
    if not SPIDER_ZIP.exists():
        return {"skipped": "キャッシュにスパイダープラスの XBRL がない"}
    out = TMP / "spider.json"
    run("xbrl_to_data.py", SPIDER_ZIP, "--pdf-url", "https://www.release.tdnet.info/inbs/140120261111574195.pdf", "--out", out)
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["basis"]["unit"] == "千円" and d["cf"]["disclosed"] is False
    op = next(ln for ln in d["pl"]["lines"] if ln.get("key") == "operating_income")
    assert op["label"] == "営業利益又は営業損失（△）" and op["value"] == 182887 and op["prior"] == -58395
    assert d["forecast"]["revenue"] == 5528000, "予想（百万円）を財務諸表の単位（千円）に揃える"
    d["revenue_breakdown_unit"] = "百万円"
    d["revenue_breakdown"] = [{"label": "ストック売上高", "value": 3907, "prior": 3556, "src": "S3 p.10"},
                              {"label": "スポット売上高", "value": 164, "prior": 40, "src": "S3 p.10"}]
    out.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    t = run("fin.py", "tables", out, "--only", "segments").stdout
    assert "単位：百万円" in t and "| ストック売上高 | 3,556 | 3,907 |" in t, "内訳は開示された単位のまま表示する"
    r = js(run("diagrams.py", "build", out, "--out-dir", TMP / "fig_spider", "--prefix", "SP"))
    assert r["ok"], r
    figs = {f["figure"]: f for f in r["figures"]}
    assert set(figs) == {"PL", "BS", "SEG"}, "CF 計算書のない期は CF 図を作らない"
    pl = Path(figs["PL"]["drawio"]).read_text(encoding="utf-8")
    shown = " ".join(re.findall(r"<text[^>]*>(.*?)</text>", Path(figs["PL"]["svg"]).read_text(encoding="utf-8"), re.S))
    assert 'fra_name="営業利益"' in pl and "又は" not in shown and "営業利益" in shown, "図では符号に合わせた1つの科目名にする"
    seg = Path(figs["SEG"]["drawio"]).read_text(encoding="utf-8")
    assert 'fra_name="ストック売上高" fra_value="3907000"' in seg, "内訳を財務諸表の単位（千円）に換算して描く"
    sys.path.insert(0, str(SCRIPTS))
    import check_report
    lv = check_report.labelled_values(d)
    assert 182887 * 1000 in lv["営業利益"]["amt"] and -58395 * 1000 in lv["営業損失"]["amt"], "「又は」の科目名を分けて照合する"
    return {"pl_figure": "営業利益"}


@test
def xbrl_real_kanamic_consolidated_summary_and_nil_debt():
    """カナミック（日本基準・連結・千円）：サマリーに併記された個別の自己資本・自己資本比率で連結の値を上書きしない。
    当期に全額償還して「－」になった社債の前期残高を、前期の有利子負債に含める。"""
    if not KANAMIC_ZIP.exists():
        return {"skipped": "キャッシュにカナミックの XBRL がない"}
    out = TMP / "kanamic.json"
    run("xbrl_to_data.py", KANAMIC_ZIP, "--pdf-url", "https://www.release.tdnet.info/inbs/140120261113579807.pdf", "--out", out)
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["basis"]["unit"] == "千円" and d["period"]["type"] == "FY"
    oe = d["bs"]["owners_equity"]
    assert oe["value"] == 5790237 and oe["prior"] == 4649304, f"連結の自己資本（個別の5,745百万円ではない）: {oe}"
    ratio = next(e for e in d["extra"] if e["label"].startswith("自己資本比率"))
    assert str(ratio["values"]["2026-09-30"]) == "74.2", f"連結の自己資本比率（個別の81.0%ではない）: {ratio}"
    bond = next(ln for ln in d["bs"]["liabilities"] if "社債" in ln["label"])
    assert bond["value"] is None and bond["prior"] == 15000
    bond["nil_fields"] = ["value"]          # PDF（S1 p.9）で「－」を確認した後の状態
    out.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    m = {x["key"]: x for x in js(run("fin.py", "metrics", out))["metrics"]}
    debt = m["interest_bearing_debt"]
    assert debt["value"] == "306009" and debt["prior"] == "595437", f"前期の社債15,000千円を含める: {debt}"
    return {"debt_prior": debt["prior"]}


@test
def wrap_kinsoku_and_katakana():
    """図のラベルの折り返し：長音・小書き仮名を行頭に置かず、漢字からカタカナへの切り替わりで改行する。"""
    sys.path.insert(0, str(SCRIPTS))
    import diagrams
    w = diagrams.wrap("医療・介護クラウドプラットフォーム", 92, 11)
    assert w == "医療・介護\nクラウドプラット\nフォーム", repr(w)
    for t in ("医療・介護クラウドプラットフォーム", "スマートフォンアプリケーション", "ソリューションサービス開発"):
        for line in diagrams.wrap(t, 60, 11).split("\n"):
            assert line[0] not in diagrams.NO_BREAK_BEFORE, f"行頭禁則: {t!r} → {line!r}"
    return {"sample": w}


@test
def xbrl_pl_tax_line_without_total():
    """四半期の税金費用が「法人税、住民税及び事業税」の1行だけで合計（法人税等）の行がない場合（日本情報クリエイトで発見）：
    存在しない親の内訳にせず、税金費用（income_taxes）として段階利益の計算に含める。合計の行がある場合は従来どおり内訳にする。"""
    sys.path.insert(0, str(SCRIPTS))
    import xbrl_to_data

    def facts(rows):
        per = {"start": "2026-07-01", "end": "2026-09-30"}
        return [{"name": f"jppfs_cor:{n}", "label": lab, "value": v, "context": "Cur", "unit": "JPY", "nil": False,
                 "order": i, "period": per} for i, (n, lab, v) in enumerate(rows)]

    head = [("NetSales", "売上高", "1000"), ("OperatingIncome", "営業利益", "300"), ("OrdinaryIncome", "経常利益", "300"),
            ("IncomeBeforeIncomeTaxes", "税引前四半期純利益", "300")]
    lines, _ = xbrl_to_data.build_pl(facts(head + [("IncomeTaxesCurrent", "法人税、住民税及び事業税", "100"),
                                                   ("ProfitLoss", "四半期純利益", "200")]), None)
    tax = next(ln for ln in lines if ln["xbrl"] == "IncomeTaxesCurrent")
    assert tax.get("key") == "income_taxes" and "parent" not in tax and tax["kind"] == "expense", tax
    lines, _ = xbrl_to_data.build_pl(facts(head + [("IncomeTaxesCurrent", "法人税、住民税及び事業税", "120"),
                                                   ("IncomeTaxesDeferred", "法人税等調整額", "-20"),
                                                   ("IncomeTaxes", "法人税等合計", "100"),
                                                   ("ProfitLoss", "四半期純利益", "200")]), None)
    kids = [ln for ln in lines if ln["xbrl"] in ("IncomeTaxesCurrent", "IncomeTaxesDeferred")]
    assert all(ln.get("parent") == "income_taxes" and "key" not in ln for ln in kids), kids
    return {"ok": True}


@test
def xbrl_real_logizard_first_consolidation():
    """ロジザード：初めての連結決算（比較情報なし）・業績予想未定・無借金の表示。"""
    if not LOGIZARD_ZIP.exists():
        return {"skipped": "キャッシュにロジザードの XBRL がない"}
    out = TMP / "logizard.json"
    run("xbrl_to_data.py", LOGIZARD_ZIP, "--pdf-url", "https://www.release.tdnet.info/inbs/140120261111574017.pdf", "--out", out)
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["basis"]["unit"] == "千円" and d["basis"]["scope"] == "連結"
    assert d["review_todo"][0].startswith("前期・前年同期の値が XBRL に1つもない"), d["review_todo"][0]
    assert not any(t.startswith("XBRL に値がない欄") for t in d["review_todo"]), "全行の前期欄の羅列はしない"
    d["period"]["no_comparative_reason"] = "初めての連結決算のため"
    d["bs"]["no_debt"] = True
    d["forecast"] = {"period": "2027年6月期", "as_of": "2026-11-13", "dividend": 19, "undetermined": True,
                     "undetermined_reason": "子会社の業績予想を精査中のため"}
    out.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    v = js(run("fin.py", "validate", out))
    assert v["ok"], v["errors"]
    t = {k: run("fin.py", "tables", out, "--only", k).stdout for k in ("pl", "bs", "forecast")}
    assert "前年同期の比較情報はない（初めての連結決算のため）" in t["pl"] and "未開示" not in t["pl"], t["pl"][:400]
    assert "前期末の比較情報はない" in t["bs"] and "None" not in t["bs"] and "未開示" not in t["bs"], t["bs"][:400]
    assert "業績予想を「未定」" in t["forecast"] and "子会社の業績予想を精査中のため" in t["forecast"]
    m = {x["key"]: x for x in js(run("fin.py", "metrics", out))["metrics"]}
    assert m["interest_bearing_debt"]["prior"] is None and m["de_ratio"]["prior"] is None, "前期末の BS がなければ前期は0にしない"
    sys.path.insert(0, str(SCRIPTS))
    from diagrams import wrap
    assert wrap("1百万円（0.1%未満）", 108, 12) == "1百万円（0.1%\n未満）", "「未満」の間で改行しない"
    return {"checks": len(v["checks"])}


@test
def xbrl_real_cyberlinks_indirect_depreciation_and_ordinary_segments():
    """サイバーリンクス（日本基準）：有形固定資産の間接控除方式（取得原価・減価償却累計額・純額）を二重計上しない。
    セグメント利益が経常利益と照合される会社の SEG 図の脚注。"""
    if not CYBERLINKS_ZIP.exists():
        return {"skipped": "キャッシュにサイバーリンクスの XBRL がない"}
    out = TMP / "cyberlinks.json"
    run("xbrl_to_data.py", CYBERLINKS_ZIP, "--pdf-url", "https://www.release.tdnet.info/inbs/140120261111572905.pdf",
        "--out", out)
    d = json.loads(out.read_text(encoding="utf-8"))
    assets = d["bs"]["assets"]
    ppe_kids = [ln["label"] for ln in assets if ln.get("parent") == "ppe"]
    assert ppe_kids == ["建物及び構築物（純額）", "土地", "その他（純額）"], ppe_kids
    net = next(ln for ln in assets if ln["label"] == "建物及び構築物（純額）")
    gross_acc = [ln["label"] for ln in assets if ln.get("parent") == net["key"]]
    assert gross_acc == ["建物及び構築物", "減価償却累計額"], gross_acc
    v = js(run("fin.py", "validate", out))
    assert v["ok"], v["errors"]
    t = run("fin.py", "tables", out, "--only", "bs").stdout
    assert "| 　建物及び構築物（純額） |" in t and "| 　　減価償却累計額 |" in t, "孫の行は2段字下げ"
    d["segments"] = [
        {"name": "A事業", "revenue": 4053, "revenue_prior": 3850, "profit": 408, "profit_prior": 552,
         "profit_basis": "セグメント利益（経常利益ベース）", "profit_reconciles_to": "ordinary_income", "adjustment": -379},
        {"name": "B事業", "revenue": 10362, "revenue_prior": 9263, "profit": 1591, "profit_prior": 1105},
    ]
    out.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    r = js(run("diagrams.py", "build", out, "--out-dir", TMP / "fig_cyber", "--prefix", "C"))
    seg = next(f for f in r["figures"] if f["figure"] == "SEG")
    assert seg["verify"]["ok"], seg["verify"]["errors"]
    x = Path(seg["drawio"]).read_text(encoding="utf-8").replace("&#xa;", "")   # 折り返しの改行を除いて照合
    assert "連結の経常利益（16.2億円）になる" in x and "連結の営業利益" not in x, "経常利益と照合する脚注"
    return {"checks": len(v["checks"])}


@test
def pdf_text_pages():
    pdf = NINTENDO_ZIP.with_name("140120261027557139.pdf")
    if not pdf.exists() or not shutil.which("pdftotext"):
        return {"skipped": "PDF または pdftotext がない"}
    r = run("pdf_text.py", pdf, "--grep", "売上高は9,660億円")
    assert "p.4:" in r.stdout, r.stdout
    return {"hit": r.stdout.strip()[:60]}


@test
def tdnet_live(live=False):
    if not live:
        return {"skipped": "--live なし"}
    r = js(run("tdnet.py", "search", "--code", "7974"))
    assert r["ok"] and isinstance(r["results"], list)
    return {"count": r["count"]}


# ---------------------------------------------------------------- 3. 数値検証

@test
def validate_samples_and_detect_errors():
    for f in ("sample_jgaap_loss_fy.json", "sample_ifrs_deficit_q1.json"):
        v = js(run("fin.py", "validate", FIX / f))
        assert v["ok"], (f, v["errors"])
        assert any("サンプル" in w for w in v["warnings"])
    d = json.loads((FIX / "sample_jgaap_loss_fy.json").read_text(encoding="utf-8"))
    d["pl"]["lines"][1]["value"] = 8100          # 売上原価を書き換える → 売上総利益と合わなくなる
    d["bs"]["assets"][0]["value"] = 1600         # 現金を書き換える → 資産合計と合わなくなる
    d["cf"]["lines"][0]["value"] = -400          # 営業CFを書き換える → 期末残高と合わなくなる
    bad = TMP / "bad.json"
    bad.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    v = js(run("fin.py", "validate", bad, ok_codes=(1,)))
    msg = " ".join(v["errors"])
    assert "売上総利益" in msg and "流動資産" in msg and "期末残高" in msg, msg
    d2 = json.loads((FIX / "sample_ifrs_deficit_q1.json").read_text(encoding="utf-8"))
    d2["cf"]["lines"] = [{"key": "cfo", "label": "営業CF", "value": 1}]
    bad2 = TMP / "bad2.json"
    bad2.write_text(json.dumps(d2, ensure_ascii=False), encoding="utf-8")
    v2 = js(run("fin.py", "validate", bad2, ok_codes=(1,)))
    assert any("未開示の数値を作らない" in e for e in v2["errors"])
    return {"detected": len(v["errors"])}


@test
def metrics_loss_and_deficit():
    m = {x["key"]: x for x in js(run("fin.py", "metrics", FIX / "sample_jgaap_loss_fy.json"))["metrics"]}
    assert m["growth_operating_income"]["note"] == "赤字転落"
    assert m["equity_ratio"]["value"] == "23.7"
    assert m["fcf"]["value"] == "-1100"
    assert m["interest_bearing_debt"]["value"] == "4500" and "リース負債 100" in m["interest_bearing_debt"]["note"]
    assert m["effective_tax_rate"]["value"] is None
    assert "cfo_to_ni" not in m, "純利益が負のとき 営業CF÷純利益 は出さない"
    m2 = {x["key"]: x for x in js(run("fin.py", "metrics", FIX / "sample_ifrs_deficit_q1.json"))["metrics"]}
    assert m2["growth_operating_income"]["note"] == "赤字縮小"
    assert m2["fcf"]["value"] is None and "CF計算書が開示されていない" in m2["fcf"]["note"]
    assert m2["roe"]["annualized"] is True
    return {"n_metrics": len(m)}


@test
def quarter_and_history():
    q2 = json.loads((FIX / "sample_ifrs_deficit_q1.json").read_text(encoding="utf-8"))
    q2["period"].update({"label": "2027年3月期_第2四半期", "type": "Q2", "end": "2026-09-30", "months": 6})
    for ln in q2["pl"]["lines"]:
        if ln.get("value") is not None:
            ln["value"] = ln["value"] * 2
    (TMP / "hist").mkdir(exist_ok=True)
    p1, p2 = TMP / "hist" / "2027年3月期_第1四半期.json", TMP / "hist" / "2027年3月期_第2四半期.json"
    shutil.copy(FIX / "sample_ifrs_deficit_q1.json", p1)
    p2.write_text(json.dumps(q2, ensure_ascii=False), encoding="utf-8")
    q = js(run("fin.py", "quarter", p2, p1))
    rev = next(r for r in q["pl"] if r["key"] == "revenue")
    assert q["ok"] and rev["standalone"] == "5000", q
    q2b = dict(q2, basis=dict(q2["basis"], standard="J-GAAP"))
    p3 = TMP / "q2b.json"
    p3.write_text(json.dumps(q2b, ensure_ascii=False), encoding="utf-8")
    bad = js(run("fin.py", "quarter", p3, p1))
    assert not bad["ok"] and "basis.standard" in bad["problems"][0]
    # 本決算の実績と、過去の四半期時点の会社予想の比較
    fy = json.loads((FIX / "sample_jgaap_loss_fy.json").read_text(encoding="utf-8"))
    fy.update(company=q2["company"], basis=q2["basis"])
    fy["period"].update({"label": "2027年3月期_本決算", "fiscal_year": "2027年3月期", "start": "2026-04-01", "end": "2027-03-31"})
    (TMP / "hist" / "2027年3月期_本決算.json").write_text(json.dumps(fy, ensure_ascii=False), encoding="utf-8")
    h = js(run("fin.py", "history", TMP / "hist"))
    assert [p["label"] for p in h["periods"]] == ["2027年3月期_第1四半期", "2027年3月期_第2四半期", "2027年3月期_本決算"]
    fva = [x for x in h["forecast_vs_actual"] if x["item"] == "revenue"]
    assert fva and fva[0]["forecast"] == "21000" and fva[0]["actual"] == "10000"
    return {"forecast_vs_actual": len(h["forecast_vs_actual"])}


# ---------------------------------------------------------------- 4〜6. 図

@test
def diagrams_loss_cf():
    out = TMP / "fig1"
    r = js(run("diagrams.py", "build", FIX / "sample_jgaap_loss_fy.json", "--out-dir", out, "--prefix", "T"))
    assert r["ok"], r
    figs = {f["figure"]: f for f in r["figures"]}
    assert set(figs) == {"PL", "BS", "CF", "SEG"}
    seg = Path(figs["SEG"]["drawio"]).read_text(encoding="utf-8")
    assert 'fra_pie="revenue"' in seg, "売上高の構成を円グラフで描く"
    assert 'fra_pie="profit"' not in seg, "黒字のセグメントが1つなら利益の円グラフは作らない"
    for f in figs.values():
        assert f["verify"]["ok"], f["verify"]["errors"]
        assert Path(f["drawio"]).exists() and Path(f["svg"]).exists()
        svg = Path(f["svg"]).read_text(encoding="utf-8")
        assert "light-dark(" not in svg and "<mxfile" in svg.replace("&lt;", "<"), "白背景固定・図データの埋め込み"
    pl = Path(figs["PL"]["drawio"]).read_text(encoding="utf-8")
    assert 'fra_kind="loss"' in pl and 'fra_name="営業損失"' in pl, "営業損失を損失ブロックとして描く"
    assert 'fra_kind="profit"' not in pl.split("② ")[0] if "② " in pl else True
    assert figs["CF"]["cf_end_diff"] == "0", "CF の期首＋増減＝期末"
    labels = {v["label"]: v["display"] for v in figs["CF"]["values"]}
    assert labels["フリーキャッシュフロー"] == "△11.0億円"
    return {"sizes": {k: v["size"] for k, v in figs.items()}}


@test
def diagrams_cf_nil_line():
    """CF の換算差額などが原資料で「－」（XBRL の nil）の場合（うるるで発見）：棒を描かずに CF 図を作り、脚注に示す。
    nil ではない未取得の値（null だけ）は従来どおり CF 図を作らない（0 で補完しない）。"""
    d = json.loads((FIX / "sample_jgaap_loss_fy.json").read_text(encoding="utf-8"))
    fx = next(ln for ln in d["cf"]["lines"] if ln.get("key") == "fx")
    fx.update({"value": None, "prior": None, "nil_fields": ["value", "prior"]})
    d["cf"]["end"].update({"value": 1530, "prior": 2580})   # 期首＋営業・投資・財務の合計（換算差額なし）
    p = TMP / "cf_nil.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    r = js(run("diagrams.py", "build", p, "--out-dir", TMP / "fig_cf_nil", "--prefix", "T"))
    assert r["ok"], r
    cf = next(f for f in r["figures"] if f["figure"] == "CF")
    assert cf["verify"]["ok"], cf["verify"]["errors"]
    assert cf["cf_end_diff"] == "0", cf
    x = Path(cf["drawio"]).read_text(encoding="utf-8")
    assert 'fra_name="現金及び現金同等物に係る換算差額"' not in x, "nil の行は棒を描かない"
    assert "該当なし" in x, "棒を描かなかった項目を脚注に示す"
    del fx["nil_fields"]                                     # nil の印がない null は未取得の値
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    r = js(run("diagrams.py", "build", p, "--out-dir", TMP / "fig_cf_nil2", "--prefix", "T", ok_codes=(0, 1)))
    assert any(x["figure"] == "CF" for x in r["skipped"]), "未取得の値を 0 で補完して CF 図を作らない"
    return {"skipped_without_nil": [x["reason"] for x in r["skipped"]]}


@test
def diagrams_deficit_no_cf():
    out = TMP / "fig2"
    r = js(run("diagrams.py", "build", FIX / "sample_ifrs_deficit_q1.json", "--out-dir", out))
    assert r["ok"], r
    skipped = {x["figure"]: x["reason"] for x in r["skipped"]}
    assert "CF" in skipped and "第1四半期" in skipped["CF"], "CF 未開示なら CF 図を作らず理由を返す"
    assert not any(f["figure"] == "CF" for f in r["figures"])
    bs = next(f for f in r["figures"] if f["figure"] == "BS")
    x = Path(bs["drawio"]).read_text(encoding="utf-8")
    assert 'fra_kind="deficit"' in x, "債務超過を資産側の不足額として描く"
    # 左右の高さが等しいこと（資産＋債務超過額 ＝ 負債）
    hs = {"L": 0.0, "R": 0.0}
    for m in re.finditer(r'<object [^>]*fra_value="([^"]+)" fra_scale="([^"]+)" fra_kind="(bs_item|deficit)"[^>]*>.*?<mxGeometry x="([\d.]+)"', x, re.S):
        h = abs(float(m.group(1))) * float(m.group(2))
        hs["L" if float(m.group(4)) < 400 else "R"] += h
    assert abs(hs["L"] - hs["R"]) < 1.0, hs
    return {"heights": hs}


@test
def diagrams_detect_tampering():
    out = TMP / "fig1"
    p = out / "T_PL.drawio"
    if not p.exists():
        run("diagrams.py", "build", FIX / "sample_jgaap_loss_fy.json", "--out-dir", out, "--prefix", "T")
    x = p.read_text(encoding="utf-8")
    x2 = re.sub(r'(fra_name="売上原価".*?height=")([\d.]+)"', lambda m: f'{m.group(1)}{float(m.group(2)) + 20:.2f}"', x, count=1, flags=re.S)
    t = out / "tampered.drawio"
    t.write_text(x2, encoding="utf-8")
    shutil.copy(out / "T_PL.svg", out / "tampered.svg")
    r = js(run("diagrams.py", "verify", t, ok_codes=(1,)))
    assert any("売上原価" in e for e in r[0]["errors"]), r
    return {"detected": r[0]["errors"][0]}


@test
def diagrams_seg_pie_and_tampering():
    """セグメント図：売上高と利益の円グラフ。赤字セグメントは利益の円に含めない。扇形の改ざんを検出する。"""
    d = json.loads((FIX / "sample_jgaap_loss_fy.json").read_text(encoding="utf-8"))
    d["segments"].append({"name": "部品販売（サンプル）", "revenue": 1000, "revenue_prior": 900, "profit": 300,
                          "profit_prior": 250, "src": "S1 p.10"})
    src = TMP / "seg3.json"
    src.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    out = TMP / "fig_seg"
    r = js(run("diagrams.py", "build", src, "--out-dir", out, "--prefix", "S"))
    seg = next(f for f in r["figures"] if f["figure"] == "SEG")
    assert seg["verify"]["ok"], seg["verify"]["errors"]
    x = Path(seg["drawio"]).read_text(encoding="utf-8")
    rev = re.findall(r'fra_pie="revenue"', x)
    pro = re.findall(r'<object[^>]*fra_name="([^"]+)"[^>]*fra_pie="profit"', x)
    assert len(rev) == 3, "売上高の円は全セグメント"
    assert sorted(pro) == ["保守サービス（サンプル）", "部品販売（サンプル）"], f"赤字の産業機械は利益の円に含めない: {pro}"
    assert "構成比を表せないため利益の円に含めない" in x
    # 扇形の割合：保守サービス 200 ÷ 黒字合計 500 ＝ 0.4
    m = re.search(r'fra_name="保守サービス（サンプル）"[^>]*fra_pie="profit"[^>]*><mxCell style="[^"]*startAngle=([\d.]+);endAngle=([\d.]+)', x)
    assert m and abs(float(m.group(2)) - float(m.group(1)) - 0.4) < 1e-6, m and m.groups()
    t = out / "tampered_seg.drawio"
    t.write_text(x.replace(f"endAngle={m.group(2)}", "endAngle=0.500000", 1), encoding="utf-8")
    shutil.copy(Path(seg["svg"]), out / "tampered_seg.svg")
    v = js(run("diagrams.py", "verify", t, ok_codes=(1,)))
    assert any("扇形の割合" in e for e in v[0]["errors"]) and any("隙間または重なり" in e for e in v[0]["errors"]), v
    return {"profit_slices": len(pro), "detected": v[0]["errors"][0]}


@test
def check_report_revenue_breakdown_unit():
    """売上内訳（revenue_breakdown）の値を本文照合に使い、revenue_breakdown_unit の単位で円に換算する。"""
    sys.path.insert(0, str(SCRIPTS))
    import check_report
    d = json.loads((FIX / "sample_jgaap_loss_fy.json").read_text(encoding="utf-8"))
    d["segments"] = []
    d["basis"]["unit"] = "千円"
    d["revenue_breakdown_unit"] = "百万円"
    d["revenue_breakdown"] = [{"label": "月額利用料等（サンプル）", "value": 2863, "prior": 2333, "src": "S1 p.4"},
                              {"label": "機器販売等（サンプル）", "value": 618, "prior": 577, "src": "S1 p.4"}]
    lv = check_report.labelled_values(d)
    e = lv["月額利用料等（サンプル）"]
    assert 2863 * 1_000_000 in e["amt"] and 2333 * 1_000_000 in e["amt"], "百万円単位の内訳を円に換算して照合する"
    assert any(abs(float(x) - 22.7) < 0.05 for x in e["pct"]), e["pct"]
    del d["revenue_breakdown_unit"]
    lv2 = check_report.labelled_values(d)
    assert 2863 * 1_000 in lv2["月額利用料等（サンプル）"]["amt"], "単位の指定がなければ財務諸表の単位（千円）"
    return {"names": 2}


@test
def download_same_filename_different_url():
    """同じファイル名（00.pdf など）で URL が異なる資料を、上書き・流用せず別々に保存し、取得記録も正しく残す。"""
    sys.path.insert(0, str(SCRIPTS))
    import fra_common
    body = {"https://example.invalid/doc/1/00.pdf": b"%PDF-one", "https://example.invalid/doc/2/00.pdf": b"%PDF-two"}
    orig = fra_common.http_get
    fra_common.http_get = lambda url, **kw: body[url]
    try:
        dd = TMP / "dl_same_name"
        a = fra_common.download("https://example.invalid/doc/1/00.pdf", dd)
        b = fra_common.download("https://example.invalid/doc/2/00.pdf", dd)
        a2 = fra_common.download("https://example.invalid/doc/1/00.pdf", dd)
    finally:
        fra_common.http_get = orig
    assert a["path"] != b["path"], "別の URL の同名ファイルを流用しない"
    assert Path(a["path"]).read_bytes() == b"%PDF-one" and Path(b["path"]).read_bytes() == b"%PDF-two"
    assert not b["cached"] and a2["cached"] and a2["path"] == a["path"], "同じ URL は再取得しない"
    metas = {json.loads(m.read_text(encoding="utf-8"))["url"] for m in dd.glob(".*.meta.json")}
    assert metas == set(body), f"取得記録の URL が正しく残る: {metas}"
    return {"files": sorted(Path(x["path"]).name for x in (a, b))}


@test
def diagrams_bs_merges_other_with_suffix():
    """BS 図：「その他（流動）」のように括弧書きが付いた「その他」科目も集約ブロックにまとめる（システム ディで発見）。"""
    d = json.loads((FIX / "sample_jgaap_loss_fy.json").read_text(encoding="utf-8"))
    for ln in d["bs"]["liabilities"]:
        if ln["label"] == "その他" and ln.get("group") == "流動負債":
            ln["label"] = "その他（流動）"
    src = TMP / "bs_other.json"
    src.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    r = js(run("diagrams.py", "build", src, "--out-dir", TMP / "fig_bs_other", "--prefix", "B"))
    bs = next(f for f in r["figures"] if f["figure"] == "BS")
    assert bs["verify"]["ok"], bs["verify"]["errors"]
    x = Path(bs["drawio"]).read_text(encoding="utf-8")
    assert 'fra_name="その他（流動）"' not in x, "括弧書き付きの「その他」を単独のブロックにしない"
    assert "流動負債の「その他」＝" in x and "その他（流動）" in x, "集約の内訳に科目名を書く"
    return {"ok": True}


# ---------------------------------------------------------------- 7〜9. Obsidian

def make_note(code, name, fixture, period, bm_fixture="sample_bizmodel_small.json"):
    run("obsidian.py", "init-company", "--code", code, "--name", name, "--industry", "サンプル", "--sample")
    paths = js(run("obsidian.py", "paths", "--code", code, "--period", period))
    cd = Path(paths["company_dir"])
    dj = cd / "data" / f"{period}.json"
    shutil.copy(FIX / fixture, dj)
    bmj = Path(paths["bizmodel"])
    bmj.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIX / bm_fixture, bmj)
    r = js(run("diagrams.py", "build", dj, "--out-dir", cd / "assets", "--prefix", period))
    assert r["bizmodel"]["ok"], r["bizmodel"]
    emb = run("obsidian.py", "embeds", "--code", code, "--period", period).stdout
    figs = {m.group(1): m.group(0) for m in re.finditer(r"<!-- fra:fig:([\w-]+) -->\n.*?\n\*.*?\*", emb, re.S)}
    t = {k: run("fin.py", "tables", dj, "--only", k).stdout.strip() for k in ("pl", "bs", "cf", "metrics", "segments", "forecast")}
    t["bizmodel"] = run("bizmodel.py", "table", bmj).stdout.strip()
    d = json.loads(dj.read_text(encoding="utf-8"))
    bm = json.loads(bmj.read_text(encoding="utf-8"))
    body = "\n\n".join([
        f"# {name} {period}", "## A. 3分でわかる決算概要",
        "【事実】売上高は100億円（前期比△16.7%）。【解釈】固定費の負担で営業損失。【仮説】受注回復で改善する。判断材料不足の点がある。",
        "## B. ビジネスモデル", figs.get("BM", ""), t["bizmodel"], t["segments"], "## C. 損益計算書（PL）", figs.get("PL", ""), t["pl"],
        "## D. 貸借対照表（BS）", figs.get("BS", ""), t["bs"], "## E. キャッシュ・フロー計算書（CF）", figs.get("CF", ""), t["cf"],
        "## F. 7つの観点からの分析", t["metrics"], t["forecast"], "## H. まとめ", "### 今回の決算からわかったこと",
        "### まだ判断できないこと", "### 次回決算で確認すべき指標", "## 参考資料・出典",
        "\n".join(f"- {s['id']}. {s['title']} {s['url']}" for s in d["sources"] + bm.get("sources", []))])
    fm = {"会社名": name, "証券コード": code, "業種": "サンプル", "決算期": period.replace("_", " "), "対象期間": "x",
          "資料公表日": d["period"]["published"], "分析日": "2026-11-18", "会計基準": d["basis"]["standard"],
          "連結・単体": d["basis"]["scope"], "tags": ["決算分析", "サンプル"], "fra_type": "period_report",
          "fra_period": period, "fra_sample": "true"}
    (TMP / "body.md").write_text(body, encoding="utf-8")
    (TMP / "fm.json").write_text(json.dumps(fm, ensure_ascii=False), encoding="utf-8")
    note = cd / f"{period}.md"
    return note, dj, cd


@test
def obsidian_create_update_dedupe_memo():
    setup_env() if not (VAULT / ".obsidian").exists() else None
    note, dj, cd = make_note("0000", "サンプル赤字工業株式会社（架空）", "sample_jgaap_loss_fy.json", "2026年3月期_本決算")
    r1 = js(run("obsidian.py", "upsert", "--note", note, "--body", TMP / "body.md", "--frontmatter", TMP / "fm.json"))
    assert r1["action"] == "created"
    r2 = js(run("obsidian.py", "upsert", "--note", note, "--body", TMP / "body.md", "--frontmatter", TMP / "fm.json"))
    assert r2["action"] == "unchanged", "同じ内容の再実行でファイルを変えない"
    # 利用者のメモと frontmatter の追記
    t = note.read_text(encoding="utf-8")
    t = t.replace("\n---\n", "\nmy_note: 重要\n---\n", 1) + "\n利用者のメモ：次回は受注を確認\n"
    note.write_text(t, encoding="utf-8")
    (TMP / "body.md").write_text((TMP / "body.md").read_text(encoding="utf-8") + "\n追記した分析。", encoding="utf-8")
    r3 = js(run("obsidian.py", "upsert", "--note", note, "--body", TMP / "body.md", "--frontmatter", TMP / "fm.json"))
    assert r3["action"] == "updated"
    t = note.read_text(encoding="utf-8")
    assert "利用者のメモ：次回は受注を確認" in t and "my_note: 重要" in t and "追記した分析。" in t
    assert len(list(cd.glob("*.md"))) == 2, "決算ノートと企業概要の2つだけ（重複なし）"
    # 自動生成部分の人手編集 → 中止（exit 4）
    note.write_text(t.replace("【解釈】固定費の負担で営業損失。", "【解釈】固定費の負担で営業損失（人が加筆）。"), encoding="utf-8")
    r4 = js(run("obsidian.py", "upsert", "--note", note, "--body", TMP / "body.md", "--frontmatter", TMP / "fm.json", ok_codes=(4,)))
    assert r4["action"] == "aborted_manual_edit" and "人が加筆" in note.read_text(encoding="utf-8")
    r5 = js(run("obsidian.py", "upsert", "--note", note, "--body", TMP / "body.md", "--frontmatter", TMP / "fm.json", "--force"))
    assert r5["backup"] and Path(r5["backup"]).exists(), "--force でも元のノートをバックアップする"
    return {"actions": [r1["action"], r2["action"], r3["action"], r4["action"], "forced"]}


@test
def obsidian_second_period_and_links():
    if not (VAULT / ".obsidian").exists():
        setup_env()
    note1, *_ = make_note("0000", "サンプル赤字工業株式会社（架空）", "sample_jgaap_loss_fy.json", "2026年3月期_本決算")
    if not note1.exists():
        run("obsidian.py", "upsert", "--note", note1, "--body", TMP / "body.md", "--frontmatter", TMP / "fm.json")
    q = json.loads((FIX / "sample_jgaap_loss_fy.json").read_text(encoding="utf-8"))
    note2, dj2, cd = make_note("0000", "サンプル赤字工業株式会社（架空）", "sample_jgaap_loss_fy.json", "2027年3月期_第1四半期")
    run("obsidian.py", "upsert", "--note", note2, "--body", TMP / "body.md", "--frontmatter", TMP / "fm.json")
    rl = js(run("obsidian.py", "relink", "--code", "0000"))
    assert len(rl["period_notes"]) == 2
    t2 = note2.read_text(encoding="utf-8")
    assert "前の決算：[[決算分析/サンプル赤字工業株式会社（架空）(0000)/2026年3月期_本決算|" in t2
    t1 = note1.read_text(encoding="utf-8")
    assert "次の決算：[[決算分析/サンプル赤字工業株式会社（架空）(0000)/2027年3月期_第1四半期|" in t1
    ov = (cd / "企業概要.md").read_text(encoding="utf-8")
    assert "2027年3月期 第1四半期" in ov and "主要数値の推移" in ov
    idx = (VAULT / "決算分析" / "企業一覧.md").read_text(encoding="utf-8")
    assert "(0000)/企業概要|" in idx and "（サンプル）" in idx
    # 埋め込み先の SVG が実在する
    for e in re.findall(r"!\[\[([^\]|]+\.svg)", t2):
        assert (VAULT / e).exists(), e
    return {"notes": [Path(p).name for p in rl["period_notes"]]}


@test
def check_report_detects_wrong_number():
    if not (VAULT / ".obsidian").exists():
        setup_env()
    note, dj, cd = make_note("0000", "サンプル赤字工業株式会社（架空）", "sample_jgaap_loss_fy.json", "2026年3月期_本決算")
    body = (TMP / "body.md").read_text(encoding="utf-8").replace("追記した分析。", "")
    (TMP / "body.md").write_text(body + "\n営業利益は△8.0億円で、売上総利益率は18.0%。\n", encoding="utf-8")
    run("obsidian.py", "upsert", "--note", note, "--body", TMP / "body.md", "--frontmatter", TMP / "fm.json", "--force")
    c = js(run("check_report.py", "--note", note, "--data", dj, "--vault", VAULT))
    assert c["ok"] and not c["warnings"], c
    (TMP / "body.md").write_text(body + "\n誤記：営業利益は12億円。\n", encoding="utf-8")
    run("obsidian.py", "upsert", "--note", note, "--body", TMP / "body.md", "--frontmatter", TMP / "fm.json", "--force")
    c2 = js(run("check_report.py", "--note", note, "--data", dj, "--vault", VAULT))
    assert any("12億円" in w and "営業利益" in w for w in c2["warnings"]), c2["warnings"]
    # 表の手書き改変の検出
    t = note.read_text(encoding="utf-8").replace("| 売上高 | 12,000 |", "| 売上高 | 12,500 |")
    note.write_text(t, encoding="utf-8")
    c3 = js(run("check_report.py", "--note", note, "--data", dj, "--vault", VAULT, ok_codes=(1,)))
    assert any("fra:table:pl" in e for e in c3["errors"]), c3["errors"]
    return {"warnings": len(c2["warnings"])}


@test
def refresh_tables_keeps_text():
    if not (VAULT / ".obsidian").exists():
        setup_env()
    note, dj, cd = make_note("0000", "サンプル赤字工業株式会社（架空）", "sample_jgaap_loss_fy.json", "2026年3月期_本決算")
    (TMP / "body.md").write_text((TMP / "body.md").read_text(encoding="utf-8").replace("誤記：営業利益は12億円。", "") +
                                 "\n独自の文章。\n", encoding="utf-8")
    run("obsidian.py", "upsert", "--note", note, "--body", TMP / "body.md", "--frontmatter", TMP / "fm.json", "--force")
    d = json.loads(dj.read_text(encoding="utf-8"))
    d["pl"]["lines"][0]["prior"] = 12001          # データを修正
    dj.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    r = js(run("obsidian.py", "refresh-tables", "--note", note, "--data", dj))
    assert r["action"] == "updated" and "pl" in r["tables_changed"], r
    t = note.read_text(encoding="utf-8")
    assert "| 売上高 | 12,001 |" in t and "独自の文章。" in t
    r2 = js(run("obsidian.py", "refresh-tables", "--note", note, "--data", dj))
    assert r2["action"] == "unchanged"
    return {"changed": r["tables_changed"]}


# ---------------------------------------------------------------- ビジネスモデル図

def bm_fixture(name="sample_bizmodel_small.json"):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def write_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    return Path(path)


@test
def bizmodel_validate_detects_errors():
    ok = js(run("bizmodel.py", "validate", FIX / "sample_bizmodel_small.json", "--data", FIX / "sample_jgaap_loss_fy.json"))
    assert ok["ok"] and not ok["warnings"], ok
    cases = {}
    b = bm_fixture(); b["edges"][0]["to"] = "nobody"
    cases["存在しない主体"] = b
    b = bm_fixture(); b["nodes"][1]["source"] = ""
    cases["出典（source）がない"] = b
    b = bm_fixture(); b["edges"][2]["source"] = "S9 p.1"
    cases["出典 S9"] = b
    b = bm_fixture(); b["nodes"][1]["role"] = "self"
    cases["ちょうど1つ"] = b
    b = bm_fixture(); b["edges"][8].pop("note")
    cases["推測の根拠"] = b
    b = bm_fixture(); b["edges"][0]["type"] = "goods"
    cases["type「goods」"] = b
    b = bm_fixture(); b["edges"].append(dict(b["edges"][0]))
    cases["重複"] = b
    b = bm_fixture(); b["edges"][0]["from"] = b["edges"][0]["to"]
    cases["from と to が同じ"] = b
    big = bm_fixture("sample_bizmodel_large.json"); big["groups"] = []
    for n in big["nodes"]:
        n.pop("groups", None)
    for e in big["edges"]:
        e.pop("group", None)
    cases["groups（事業）を定義"] = big
    found = {}
    for key, obj in cases.items():
        pth = write_json(TMP / "bmcase" / "case.json", obj)
        r = js(run("bizmodel.py", "validate", pth, ok_codes=(1,)))
        assert not r["ok"] and any(key in x for x in r["errors"]), (key, r["errors"])
        found[key] = len(r["errors"])
    return {"detected": list(found)}


def _drawio_edges(path):
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    nodes = {o.get("id"): o for o in root.iter("object") if o.get("fra_node")}
    edges = {int(o.get("fra_edge")): o for o in root.iter("object") if o.get("fra_edge")}
    return root, nodes, edges


@test
def bizmodel_build_small_directions_and_editability():
    out = TMP / "bm_small"
    r = js(run("bizmodel.py", "build", FIX / "sample_bizmodel_small.json", "--data", FIX / "sample_jgaap_loss_fy.json",
               "--out-dir", out, "--prefix", "T"))
    assert r["ok"] and [f["figure"] for f in r["figures"]] == ["BM"], r
    f = r["figures"][0]
    assert f["verify"]["ok"], f["verify"]
    bm = bm_fixture()
    root, nodes, edges = _drawio_edges(f["drawio"])
    assert {o.get("fra_node") for o in nodes.values()} == {n["id"] for n in bm["nodes"]}
    for i, e in enumerate(bm["edges"], start=1):
        c = edges[i].find("mxCell")
        assert c.get("source") == f"n-{e['from']}" and c.get("target") == f"n-{e['to']}", f"関係 {i} の向き"
        assert edges[i].get("fra_type") == e["type"] and edges[i].get("label").startswith(chr(0x2460 + i - 1))
    for o in nodes.values():
        st = o.find("mxCell").get("style")
        assert "image=data:image/svg+xml," in st and "http" not in st, "アイコンは埋め込みの SVG"
    svg = Path(f["svg"]).read_text(encoding="utf-8")
    assert "<mxfile" in svg.replace("&lt;", "<") and "light-dark(" not in svg, "SVG に編集用データを埋め込み・白背景"
    for n in bm["nodes"]:
        for line in n["name"].split("（")[0:1]:
            assert line in svg, line
    assert "関係（矢印の向き）" not in svg, "関係の一覧表は図に入れない（ノートの表に載せる）"
    rec = json.loads((out / "T_BM_figures.json").read_text(encoding="utf-8"))
    assert rec["ok"] and rec["bizmodel_sha256"]
    v = js(run("bizmodel.py", "verify", f["drawio"], "--bizmodel", FIX / "sample_bizmodel_small.json"))
    assert v["ok"], v
    tbl = run("bizmodel.py", "table", FIX / "sample_bizmodel_small.json", "--data", FIX / "sample_jgaap_loss_fy.json").stdout
    assert tbl.startswith("<!-- fra:table:bizmodel -->") and "| 製造業の顧客企業 |" in tbl
    assert "取引関係" not in tbl and "機械の稼働データ" not in tbl, "取引関係の表はノートに載せない（JSON に残す）"
    return {"size": f["size"], "crossings": f["verify"]["n_crossings"]}


@test
def bizmodel_verify_detects_tampering():
    out = TMP / "bm_small"
    if not (out / "T_BM.drawio").exists():
        run("bizmodel.py", "build", FIX / "sample_bizmodel_small.json", "--data", FIX / "sample_jgaap_loss_fy.json",
            "--out-dir", out, "--prefix", "T")
    src = (out / "T_BM.drawio").read_text(encoding="utf-8")
    bmf = FIX / "sample_bizmodel_small.json"
    # 1) 矢印の向きを逆にする
    t = src.replace('source="n-parts" target="n-self"', 'source="n-self" target="n-parts"', 1)
    assert t != src
    (out / "X_BM.drawio").write_text(t, encoding="utf-8")
    v1 = js(run("bizmodel.py", "verify", out / "X_BM.drawio", "--bizmodel", bmf, ok_codes=(1,)))
    assert any("向き" in e for e in v1["errors"]), v1["errors"]
    # 2) 主体を別の主体に重ねる
    import xml.etree.ElementTree as ET
    root = ET.fromstring(src)
    geo = {o.get("fra_node"): o.find("mxCell/mxGeometry") for o in root.iter("object") if o.get("fra_node")}
    geo["dealer"].set("x", geo["maker"].get("x"))
    geo["dealer"].set("y", geo["maker"].get("y"))
    (out / "X_BM.drawio").write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
    v2 = js(run("bizmodel.py", "verify", out / "X_BM.drawio", "--bizmodel", bmf, ok_codes=(1,)))
    assert any("重なっている" in e for e in v2["errors"]), v2["errors"]
    # 3) 外部画像のアイコン
    t = re.sub(r"image=data:image/svg\+xml,[A-Za-z0-9+/=]+", "image=https://example.com/a.png", src, count=1)
    (out / "X_BM.drawio").write_text(t, encoding="utf-8")
    v3 = js(run("bizmodel.py", "verify", out / "X_BM.drawio", "--bizmodel", bmf, ok_codes=(1,)))
    assert any("外部画像" in e for e in v3["errors"]), v3["errors"]
    # 4) 関係の矢印を消す
    t = re.sub(r'<object id="e3".*?</object>', "", src, count=1, flags=re.S)
    (out / "X_BM.drawio").write_text(t, encoding="utf-8")
    v4 = js(run("bizmodel.py", "verify", out / "X_BM.drawio", "--bizmodel", bmf, ok_codes=(1,)))
    assert any("関係 3" in e and "ない" in e for e in v4["errors"]), v4["errors"]
    return {"detected": 4}


@test
def bizmodel_large_split_and_rebuild():
    out = TMP / "bm_large"
    r = js(run("bizmodel.py", "build", FIX / "sample_bizmodel_large.json", "--out-dir", out, "--prefix", "L"))
    assert r["ok"], r
    figs = {f["figure"]: f for f in r["figures"]}
    assert set(figs) == {"BM", "BM-ec", "BM-ads", "BM-pay"}, list(figs)
    bm = bm_fixture("sample_bizmodel_large.json")
    covered = set()
    for f in figs.values():
        assert f["verify"]["ok"], (f["figure"], f["verify"]["errors"])
        assert len(f["nodes"]) <= 10 and len(f["edges"]) <= 16
        _, _, edges = _drawio_edges(f["drawio"])
        assert set(edges) == set(f["edges"])
        for num, o in edges.items():
            assert o.get("label").startswith(chr(0x2460 + num - 1)), "番号は全体で共通"
        covered |= set(f["edges"])
    assert covered == set(range(1, len(bm["edges"]) + 1)), "すべての関係がどれかの図に入る"
    # 再分析：決済事業をなくすと 1 枚に収まり、前回の事業別の図は削除される
    drop = {n["id"] for n in bm["nodes"] if "pay" in (n.get("groups") or [])}
    bm["groups"] = [g for g in bm["groups"] if g["id"] != "pay"]
    bm["nodes"] = [n for n in bm["nodes"] if n["id"] not in drop]
    bm["edges"] = [e for e in bm["edges"] if e["from"] not in drop and e["to"] not in drop]
    pth = write_json(TMP / "bm_large_src" / "L.json", bm)
    r2 = js(run("bizmodel.py", "build", pth, "--out-dir", out, "--prefix", "L"))
    assert r2["ok"] and [f["figure"] for f in r2["figures"]] == ["BM"], r2
    assert not (out / "L_BM-pay.svg").exists() and not (out / "L_BM-ec.drawio").exists(), "前回の分割図を残さない"
    return {"split": sorted(figs), "after": [f["figure"] for f in r2["figures"]]}


@test
def bizmodel_layout_few_and_many_nodes():
    base = {"schema": "fra-bizmodel", "schema_version": 1, "sample": True, "draft": False,
            "company": {"name": "サンプル配置株式会社（架空）", "code": "0002"}, "period": "2026年3月期_本決算",
            "model_type": "btob_saas", "layout": {"split": "auto"}, "groups": [],
            "sources": [{"id": "S1", "title": "【サンプル】架空の資料", "url": "https://example.com/x.pdf", "published": "2026-05-01"}]}
    few = dict(base, nodes=[
        {"id": "self", "name": "当社（架空）", "type": "company", "role": "self", "description": "x", "source": "S1 p.1"},
        {"id": "c", "name": "法人顧客", "type": "company", "role": "customer", "description": "x", "source": "S1 p.1"}],
        edges=[{"from": "self", "to": "c", "type": "service", "label": "クラウドサービス", "source": "S1 p.1", "evidence_level": "fact"},
               {"from": "c", "to": "self", "type": "money", "label": "月額利用料", "source": "S1 p.1", "evidence_level": "fact"}])
    nodes = [{"id": "self", "name": "当社（架空）", "type": "company", "role": "self", "description": "x", "source": "S1 p.1"},
             {"id": "inv", "name": "金融機関", "type": "financial", "role": "investor", "description": "x", "source": "S1 p.1"},
             {"id": "ch", "name": "販売パートナー", "type": "company", "role": "channel", "description": "x", "source": "S1 p.1"}]
    edges = [{"from": "inv", "to": "self", "type": "money", "label": "借入金", "source": "S1 p.1", "evidence_level": "fact"},
             {"from": "self", "to": "ch", "type": "service", "label": "製品の卸売", "source": "S1 p.1", "evidence_level": "fact"}]
    for i, (nm, tp) in enumerate([("データセンター", "cloud"), ("開発委託先", "company"), ("部材の仕入先", "factory")]):
        nodes.append({"id": f"s{i}", "name": nm, "type": tp, "role": "supplier", "description": "x", "source": "S1 p.2"})
        edges += [{"from": f"s{i}", "to": "self", "type": "service", "label": f"{nm}のサービス"[:10], "source": "S1 p.2", "evidence_level": "fact"},
                  {"from": "self", "to": f"s{i}", "type": "money", "label": "利用料", "source": "S1 p.2", "evidence_level": "interpretation"}]
    for i, (nm, tp) in enumerate([("中小企業", "company"), ("医療機関", "facility"), ("自治体", "government"), ("個人利用者", "person")]):
        nodes.append({"id": f"c{i}", "name": nm, "type": tp, "role": "customer", "description": "x", "source": "S1 p.3"})
        edges.append({"from": "self", "to": f"c{i}", "type": "service", "label": "サービス提供", "source": "S1 p.3", "evidence_level": "fact"})
        edges.append({"from": f"c{i}", "to": "self", "type": "money", "label": "利用料", "source": "S1 p.3", "evidence_level": "fact"})
    edges = edges[:16]
    many = dict(base, nodes=nodes, edges=edges)
    res = {}
    for name, obj in (("few", few), ("many", many)):
        pth = write_json(TMP / "bm_layout" / f"{name}.json", obj)
        r = js(run("bizmodel.py", "build", pth, "--out-dir", TMP / "bm_layout" / "out", "--prefix", name))
        assert r["ok"], (name, r.get("error"), [f["verify"]["errors"] for f in r["figures"]])
        assert len(r["figures"]) == 1 and r["figures"][0]["verify"]["ok"]
        res[name] = {"size": r["figures"][0]["size"], "crossings": r["figures"][0]["verify"]["n_crossings"]}
    return res


@test
def bizmodel_layout_holding_company_businesses():
    """当社（持株会社）が運営する事業が当社と顧客とだけ取引する場合は、当社 → 事業 → 顧客 の列に並び、矢印が交差しない。
    説明の上を他の矢印が通る箇所は警告として報告される（エラーにはしない）。"""
    nodes = [{"id": "self", "name": "持株会社（架空）", "type": "company", "role": "self", "description": "x", "source": "S1 p.1"},
             {"id": "c", "name": "個人顧客", "type": "people", "role": "customer", "description": "x", "source": "S1 p.1"}]
    edges = []
    for i, nm in enumerate(["電力事業", "通信事業", "保険事業", "金融事業"]):
        nodes.append({"id": f"b{i}", "name": nm, "type": "business", "role": "group", "description": "x", "source": "S1 p.2"})
        edges.append({"from": f"b{i}", "to": "c", "type": "service", "label": f"{nm[:2]}の提供", "source": "S1 p.2", "evidence_level": "fact"})
    for i in range(4):
        edges.append({"from": "self", "to": f"b{i}", "type": "operation", "label": "経営管理", "source": "S1 p.1", "evidence_level": "fact"})
    obj = {"schema": "fra-bizmodel", "schema_version": 1, "sample": True, "draft": False,
           "company": {"name": "サンプル持株会社（架空）", "code": "0003"}, "period": "2026年3月期_本決算",
           "model_type": "other", "layout": {"split": "auto"}, "groups": [], "nodes": nodes, "edges": edges,
           "sources": [{"id": "S1", "title": "【サンプル】架空の資料", "url": "https://example.com/x.pdf", "published": "2026-05-01"}]}
    pth = write_json(TMP / "bm_holding" / "h.json", obj)
    r = js(run("bizmodel.py", "build", pth, "--out-dir", TMP / "bm_holding" / "out", "--prefix", "h"))
    f = r["figures"][0]
    assert r["ok"] and f["verify"]["ok"], f["verify"]
    assert f["verify"]["n_crossings"] == 0, f["verify"]
    root, nodes_x, _ = _drawio_edges(f["drawio"])
    x = {o.get("fra_node"): float(o.find("mxCell/mxGeometry").get("x")) for o in nodes_x.values()}
    assert x["self"] < min(x[f"b{i}"] for i in range(4)) and max(x[f"b{i}"] for i in range(4)) < x["c"], x
    assert all(isinstance(w, str) for w in f["verify"]["warnings"])
    return {"size": f["size"], "warnings": f["verify"]["warnings"]}


@test
def bizmodel_failure_recorded_and_reported():
    out = TMP / "bm_fail"
    b = bm_fixture(); b["edges"][0]["to"] = "nobody"
    pth = write_json(TMP / "bm_fail_src" / "F.json", b)
    r = js(run("bizmodel.py", "build", pth, "--out-dir", out, "--prefix", "F", ok_codes=(1,)))
    rec = json.loads((out / "F_BM_figures.json").read_text(encoding="utf-8"))
    assert not r["ok"] and rec["stage"] == "validate" and "存在しない主体" in rec["error"], rec
    # 財務諸表の図は、ビジネスモデル JSON が誤っていても作られる
    if not (VAULT / ".obsidian").exists():
        setup_env()
    note, dj, cd = make_note("0000", "サンプル赤字工業株式会社（架空）", "sample_jgaap_loss_fy.json", "2026年3月期_本決算")
    run("obsidian.py", "upsert", "--note", note, "--body", TMP / "body.md", "--frontmatter", TMP / "fm.json", "--force")
    c0 = js(run("check_report.py", "--note", note, "--data", dj, "--vault", VAULT))
    assert c0["ok"] and not c0["warnings"], c0
    # 生成に失敗した記録（検証・書き出しの失敗）は警告にとどめる
    recp = cd / "assets" / "2026年3月期_本決算_BM_figures.json"
    good = recp.read_text(encoding="utf-8")
    bad = json.loads(good)
    bad.update({"ok": False, "stage": "verify", "error": "ValueError: 図の検証エラー（テスト）"})
    recp.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    c1 = js(run("check_report.py", "--note", note, "--data", dj, "--vault", VAULT))
    assert c1["ok"] and any("生成に失敗" in w for w in c1["warnings"]), c1
    recp.write_text(good, encoding="utf-8")
    # ビジネスモデル JSON がない・表を書き換えた場合はエラー
    bmj = cd / "data" / "bizmodel" / "2026年3月期_本決算.json"
    t = note.read_text(encoding="utf-8")
    assert "| 製造業の顧客企業 |" in t
    note.write_text(t.replace("| 製造業の顧客企業 |", "| 製造業の顧客 |", 1), encoding="utf-8")
    c2 = js(run("check_report.py", "--note", note, "--data", dj, "--vault", VAULT, ok_codes=(1,)))
    assert any("fra:table:bizmodel" in e for e in c2["errors"]), c2["errors"]
    note.write_text(t, encoding="utf-8")
    bmj.rename(bmj.with_suffix(".bak"))
    c3 = js(run("check_report.py", "--note", note, "--data", dj, "--vault", VAULT, ok_codes=(1,)))
    bmj.with_suffix(".bak").rename(bmj)
    assert any("ビジネスモデル JSON" in e and "がない" in e for e in c3["errors"]), c3["errors"]
    return {"warning": True}


@test
def bizmodel_obsidian_overview_and_reanalysis():
    if not (VAULT / ".obsidian").exists():
        setup_env()
    period = "2026年3月期_本決算"
    note, dj, cd = make_note("0000", "サンプル赤字工業株式会社（架空）", "sample_jgaap_loss_fy.json", period)
    run("obsidian.py", "upsert", "--note", note, "--body", TMP / "body.md", "--frontmatter", TMP / "fm.json", "--force")
    ov = cd / "企業概要.md"
    ov.write_text(ov.read_text(encoding="utf-8") + "\n企業概要への利用者のメモ\n", encoding="utf-8")
    run("obsidian.py", "relink", "--code", "0000")
    t = ov.read_text(encoding="utf-8")
    latest = sorted(x.stem for x in cd.glob("*.md") if x.stem != "企業概要")[-1]
    assert "<!-- fra:bm:start -->" in t and f"{latest}_BM.svg|" in t, "企業概要に最新の決算期の全社概要図を置く"
    assert t.index("<!-- fra:bm:start -->") < t.index("<!-- fra:list:start -->") < t.index("企業概要への利用者のメモ")
    run("obsidian.py", "relink", "--code", "0000")
    assert ov.read_text(encoding="utf-8").count("<!-- fra:bm:start -->") == 1, "再実行でブロックを重複させない"
    # 再分析：関係の説明（図）と主体の説明（表）を変えて図と表を更新する
    bmj = cd / "data" / "bizmodel" / f"{period}.json"
    bm = json.loads(bmj.read_text(encoding="utf-8"))
    bm["edges"][0]["label"] = "部品の継続供給"
    bm["nodes"][1]["description"] += "（再分析で更新）"
    write_json(bmj, bm)
    c = js(run("check_report.py", "--note", note, "--data", dj, "--vault", VAULT, ok_codes=(1,)))
    assert any("更新前" in e for e in c["errors"]), "JSON 更新後の古い図を検出"
    r = js(run("diagrams.py", "build", dj, "--out-dir", cd / "assets", "--prefix", period))
    assert r["ok"] and r["bizmodel"]["ok"], r["bizmodel"]
    assert "部品の継続供給" in (cd / "assets" / f"{period}_BM.svg").read_text(encoding="utf-8")
    rt = js(run("obsidian.py", "refresh-tables", "--note", note, "--data", dj))
    assert rt["action"] == "updated" and "bizmodel" in rt["tables_changed"], rt
    c2 = js(run("check_report.py", "--note", note, "--data", dj, "--vault", VAULT))
    assert c2["ok"] and not c2["warnings"], c2
    assert "企業概要への利用者のメモ" in ov.read_text(encoding="utf-8")
    emb = run("obsidian.py", "embeds", "--code", "0000", "--period", period, "--kind", "BM").stdout
    assert "<!-- fra:fig:BM -->" in emb and "ビジネスモデル（全社概要）" in emb
    return {"overview_block": True}


@test
def vault_detection_readonly():
    r = js(run("obsidian.py", "detect"))
    assert "candidates" in r and isinstance(r["candidates"], list)
    return {"source": r["source"], "n": len(r["candidates"])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    setup_env()
    results = []
    for fn in TESTS:
        if a.only and a.only not in fn.__name__:
            continue
        try:
            info = fn(live=a.live) if fn.__name__ == "tdnet_live" else fn()
            status = "skip" if isinstance(info, dict) and "skipped" in info else "pass"
            results.append({"test": fn.__name__, "status": status, "info": info})
        except Exception as e:  # noqa: BLE001
            results.append({"test": fn.__name__, "status": "fail", "error": str(e)[:1500],
                            "trace": traceback.format_exc().splitlines()[-3:]})
        print(json.dumps(results[-1], ensure_ascii=False))
    summary = {"pass": sum(r["status"] == "pass" for r in results), "fail": sum(r["status"] == "fail" for r in results),
               "skip": sum(r["status"] == "skip" for r in results), "tmp": str(TMP) if a.keep else None}
    print(json.dumps({"summary": summary}, ensure_ascii=False))
    if not a.keep:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if summary["fail"] else 0)


if __name__ == "__main__":
    main()
