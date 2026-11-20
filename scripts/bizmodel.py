#!/usr/bin/env python3
"""ビジネスモデル JSON（主体＝nodes、取引関係＝edges）を検証し、ビジネスモデル図（draw.io と SVG）を生成する。

- 主体はアイコン（SVG を図形のスタイルに埋め込む。外部画像は使わない）と名前で表す。
- 取引関係は主体に接続した矢印で表す。矢印の色と線種は関係の種類ごとに固定（お金・商品/サービス・情報・運営/委託・所有/資本）。
- 主体の位置は役割（role）と矢印から自動で決める。業種から取引関係を補うことはしない（JSON が唯一の根拠）。
- 書き出し後に、矢印の向き（始点・終点の主体）、主体同士の重なり、矢印が他の主体を通過しないこと、
  文字の欠落・重なりを検証する。主体が多い場合は全社概要図と事業別の詳細図に分割する。

使い方:
  python3 -I bizmodel.py init     --data <data.json> --out <data_dir>/bizmodel/<決算期>.json
  python3 -I bizmodel.py validate <bizmodel.json> [--data <data.json>]
  python3 -I bizmodel.py build    <bizmodel.json> --out-dir <assets_dir> [--prefix <決算期>] [--data <data.json>] [--png]
  python3 -I bizmodel.py table    <bizmodel.json> [--data <data.json>]
  python3 -I bizmodel.py verify   <assets_dir>/<決算期>_BM.drawio --bizmodel <bizmodel.json>

--data を省略すると、<data_dir>/bizmodel/<決算期>.json に対応する <data_dir>/<決算期>.json を使う（あれば）。
出力: JSON（table は Markdown）。build は図の結果を <prefix>_BM_figures.json に保存する（失敗時も原因を保存する）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagrams import FONT, GRID, INK, LINE_H, SUB, Canvas, export, text_h, verify as verify_texts, wrap  # noqa: E402
from fra_common import VERSION, emit, wide_len  # noqa: E402

SCHEMA = "fra-bizmodel"

# ============================================================================ 語彙

NODE_TYPES = {
    "company": "企業・法人", "person": "個人・消費者", "people": "利用者・会員", "worker": "人材・求職者・従業員",
    "store": "店舗", "factory": "工場・製造拠点", "cloud": "クラウド・システム", "government": "官公庁・自治体",
    "financial": "金融機関・投資家", "facility": "施設（医療・介護・教育など）", "device": "端末・機器",
    "media": "メディア・広告媒体", "business": "事業・部門", "other": "その他",
}
ROLES = {
    "self": "分析対象の会社", "group": "グループ会社・事業部門", "supplier": "仕入先・委託先・供給側",
    "channel": "販売チャネル・仲介", "customer": "顧客", "end_user": "顧客の先の利用者", "partner": "提携先",
    "investor": "株主・親会社・資金の出し手", "other": "その他",
}
# 種類: (表示名, 色, 破線パターン)。矢印の向きは「流れの向き」（下の EDGE_DIRECTION）
EDGE_TYPES = {
    "service": ("商品・サービス", "#1565C0", None),
    "money": ("お金", "#E65100", None),
    "information": ("情報・データ", "#7B1FA2", "6 4"),
    "operation": ("運営・委託", "#2E7D32", "2 3"),
    "ownership": ("所有・資本", "#546E7A", "10 3 2 3"),
}
EDGE_DIRECTION = {
    "service": "提供する側 → 受け取る側",
    "money": "支払う側 → 受け取る側",
    "information": "送る側 → 受け取る側",
    "operation": "業務を行う側（運営者・受託者）→ 運営される側・委託元",
    "ownership": "出資する側（株主・親会社）→ 出資を受ける側",
}
EDGE_ORDER = list(EDGE_TYPES)
EVIDENCE = {"fact": "事実", "interpretation": "解釈", "hypothesis": "仮説"}
MODEL_TYPES = {
    "btob_saas": "BtoB SaaS", "btoc_platform": "BtoC プラットフォーム", "marketplace": "マーケットプレイス",
    "manufacturing_wholesale": "製造・卸売", "retail_food": "小売・飲食", "staffing": "人材紹介・派遣",
    "advertising": "広告", "finance": "金融", "other": "その他",
}
# 供給側と需要側の両方を相手にする型（当社を左右の中央に置き、供給側と需要側の直接の取引は当社を迂回して描く）
TWO_SIDED = {"btoc_platform", "marketplace", "staffing", "advertising"}
# 供給の連鎖が長くなりやすい型（仕入先・販売チャネルの段数を3段まで許す。その他は2段）
CHAIN = {"manufacturing_wholesale", "retail_food"}

ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
SRC_ID_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,3}\d{1,3})(?!\d)")
MAX_NODES, MAX_EDGES = 10, 16          # 1枚の図の上限（超えたら全社概要図と事業別の図に分ける）
LABEL_WARN_LEN = 16                     # 矢印の説明がこれより長いと警告（図では2行まで折り返す）

# ============================================================================ アイコン（48×48 の SVG。{c} は線の色）

ICONS = {
    "person": '<circle cx="24" cy="15" r="8"/><path d="M9 42c0-9 7-15 15-15s15 6 15 15z"/>',
    "people": '<circle cx="31" cy="14" r="6.5"/><path d="M19 38c0-8 5-13 12-13s12 5 12 13z"/>'
              '<circle cx="18" cy="17" r="7"/><path d="M5 42c0-8 6-13 13-13s13 5 13 13z"/>',
    "worker": '<circle cx="24" cy="14" r="8"/><path d="M9 42c0-9 7-15 15-15s15 6 15 15z"/>'
              '<path d="M24 28l-3 4 3 8 3-8z" fill="{c}"/>',
    "company": '<path d="M5 42h38"/><rect x="10" y="8" width="20" height="34"/><rect x="30" y="18" width="10" height="24"/>'
               '<path d="M15 14h3M22 14h3M15 20h3M22 20h3M15 26h3M22 26h3M33 24h4M33 30h4"/><path d="M17 42v-7h6v7"/>',
    "store": '<path d="M5 42h38"/><path d="M8 11h32l3 9H5z"/><path d="M14 11l-2 9M21 11l-1 9M27 11l1 9M34 11l2 9"/>'
             '<rect x="9" y="20" width="30" height="22"/><path d="M14 42V29h8v13"/><rect x="26" y="26" width="9" height="7"/>',
    "factory": '<path d="M5 42h38"/><path d="M7 42V22l9 6v-6l9 6v-6l9 6V9h6v33z"/><path d="M12 34h4M20 34h4M28 34h4"/>',
    "cloud": '<path d="M14 36h21a8 8 0 0 0 1.5-15.9A11 11 0 0 0 15.4 17 9.5 9.5 0 0 0 14 36z"/>'
             '<path d="M19 30h11M19 25h7"/>',
    "government": '<path d="M5 17 24 7l19 10z"/><path d="M7 21h34M7 38h34M5 42h38"/>'
                  '<path d="M11 21v17M19 21v17M29 21v17M37 21v17"/>',
    "financial": '<circle cx="24" cy="24" r="17"/><path d="M17 13l7 10 7-10M24 23v13M17.5 25.5h13M17.5 30.5h13"/>',
    "facility": '<path d="M5 42h38"/><rect x="9" y="12" width="30" height="30"/><path d="M24 17v11M18.5 22.5h11" stroke-width="3.2"/>'
                '<path d="M20 42v-8h8v8"/>',
    "device": '<rect x="7" y="9" width="34" height="23" rx="2"/><path d="M24 32v6M16 40h16"/>',
    "media": '<path d="M8 19h7l18-9v28l-18-9H8z"/><path d="M15 29l3 9h5l-3-9"/><path d="M38 18c2.5 2.5 2.5 9.5 0 12"/>',
    "business": '<rect x="6" y="16" width="36" height="24" rx="3"/><path d="M18 16v-4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v4M6 27h36"/>'
                '<path d="M21 27v4h6v-4"/>',
    "other": '<rect x="9" y="9" width="30" height="30" rx="6"/><circle cx="17" cy="24" r="2.2" fill="{c}"/>'
             '<circle cx="24" cy="24" r="2.2" fill="{c}"/><circle cx="31" cy="24" r="2.2" fill="{c}"/>',
}


def icon_data(kind: str, color: str) -> str:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">'
           f'<g fill="#FFFFFF" stroke="{color}" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round">'
           f'{ICONS[kind].replace("{c}", color)}</g></svg>')
    # draw.io のスタイルでは「;」が区切り文字のため、data URI は data:image/svg+xml,<base64> の形で書く
    return "data:image/svg+xml," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


# ============================================================================ 読み込みと検証

def load_json(p) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def default_data_path(bm_path: Path) -> Path | None:
    """<data_dir>/bizmodel/<決算期>.json → <data_dir>/<決算期>.json"""
    cand = bm_path.parent.parent / bm_path.name
    return cand if bm_path.parent.name == "bizmodel" and cand.exists() else None


def merged_sources(bm: dict, data: dict | None) -> tuple[dict, list[str]]:
    """財務データ JSON とビジネスモデル JSON の出典を id で統合する。同じ id で URL が違えばエラー。"""
    src, errs = {}, []
    for s in (data or {}).get("sources") or []:
        if s.get("id"):
            src[s["id"]] = s
    for s in bm.get("sources") or []:
        sid = s.get("id")
        if not sid:
            errs.append("sources に id のない出典がある")
            continue
        if sid in src and (src[sid].get("url") or "") != (s.get("url") or ""):
            errs.append(f"出典 {sid} が財務データ JSON の {sid} と異なる URL を指している（別の id を使うこと）")
            continue
        if not s.get("title") or not (s.get("url") or s.get("note")):
            errs.append(f"出典 {sid} に title と url（URL がない資料は note）が必要")
        src[sid] = s
    return src, errs


def src_ids(s) -> list[str]:
    return SRC_ID_RE.findall(str(s or ""))


def edges_of(bm: dict) -> list[dict]:
    """番号（JSON の順に 1 から）を付けた関係の一覧。番号は分割した図の間で共通。"""
    return [{**e, "num": i + 1} for i, e in enumerate(bm.get("edges") or [])]


def validate(bm: dict, data: dict | None = None) -> dict:
    errs, warns = [], []
    if bm.get("schema") != SCHEMA or bm.get("schema_version") != 1:
        errs.append(f'schema は "{SCHEMA}"、schema_version は 1 にする')
    comp = bm.get("company") or {}
    if not comp.get("name") or not comp.get("code"):
        errs.append("company.name（公式商号）と company.code（証券コード）が必要")
    if data:
        if str(comp.get("code", ""))[:4] != str(data["company"]["code"])[:4]:
            errs.append("company.code が財務データ JSON の証券コードと異なる")
        if bm.get("period") != data["period"]["label"]:
            warns.append(f"period（{bm.get('period')}）が財務データ JSON の決算期（{data['period']['label']}）と異なる")
    if not bm.get("period"):
        errs.append("period（決算期ラベル。例：2027年3月期_第2四半期）が必要")
    if bm.get("model_type") not in MODEL_TYPES:
        errs.append(f"model_type は {list(MODEL_TYPES)} のいずれか")
    split = (bm.get("layout") or {}).get("split", "auto")
    if split not in ("auto", "always", "never"):
        errs.append('layout.split は "auto"・"always"・"never" のいずれか')
    if bm.get("draft"):
        warns.append("draft=true のまま（レビュー後に false にする。check_report.py はエラーにする）")
    sources, serrs = merged_sources(bm, data)
    errs += serrs

    def check_src(where: str, s) -> None:
        if not str(s or "").strip():
            errs.append(f"{where} に出典（source）がない")
            return
        ids = src_ids(s)
        if not ids:
            errs.append(f"{where} の出典「{s}」に資料の id（S1 など）がない")
        for i in ids:
            if i not in sources:
                errs.append(f"{where} の出典 {i} が sources（財務データ JSON を含む）にない")

    groups = bm.get("groups") or []
    gids = set()
    for g in groups:
        if not ID_RE.match(str(g.get("id", ""))):
            errs.append(f"groups の id「{g.get('id')}」は英小文字で始まる英小文字・数字・_（32字以内）にする")
        if g.get("id") in gids:
            errs.append(f"groups の id「{g.get('id')}」が重複している")
        gids.add(g.get("id"))
        if not g.get("name"):
            errs.append(f"groups「{g.get('id')}」に name がない")
    nodes = bm.get("nodes") or []
    if not nodes:
        errs.append("nodes が空")
    ids, names = {}, {}
    for i, n in enumerate(nodes):
        nid = n.get("id")
        where = f"主体「{n.get('name') or nid or i}」"
        if not ID_RE.match(str(nid or "")):
            errs.append(f"{where} の id「{nid}」は英小文字で始まる英小文字・数字・_（32字以内）にする")
        if nid in ids:
            errs.append(f"主体の id「{nid}」が重複している")
        ids[nid] = n
        if not str(n.get("name") or "").strip():
            errs.append(f"主体「{nid}」に name がない")
        elif n["name"] in names:
            warns.append(f"主体名「{n['name']}」が重複している（図で区別できない）")
        names[n.get("name")] = nid
        if n.get("type") not in NODE_TYPES:
            errs.append(f"{where} の type「{n.get('type')}」は {list(NODE_TYPES)} のいずれか")
        if n.get("role") not in ROLES:
            errs.append(f"{where} の role「{n.get('role')}」は {list(ROLES)} のいずれか")
        if not str(n.get("description") or "").strip():
            warns.append(f"{where} に description（どのような主体か）がない")
        check_src(where, n.get("source"))
        for g in n.get("groups") or []:
            if g not in gids:
                errs.append(f"{where} の groups「{g}」が groups に定義されていない")
    selfs = [n for n in nodes if n.get("role") == "self"]
    if len(selfs) != 1:
        errs.append(f"role が self の主体（分析対象の会社）はちょうど1つにする（現在 {len(selfs)}）")
    edges = edges_of(bm)
    if not edges:
        errs.append("edges が空（取引関係が1つもない）")
    seen = set()
    for e in edges:
        where = f"関係 {e['num']}（{e.get('label') or ''}）"
        for k in ("from", "to"):
            if e.get(k) not in ids:
                errs.append(f"{where} の {k}「{e.get(k)}」が存在しない主体を参照している")
        if e.get("from") == e.get("to"):
            errs.append(f"{where} の from と to が同じ主体")
        if e.get("type") not in EDGE_TYPES:
            errs.append(f"{where} の type「{e.get('type')}」は {list(EDGE_TYPES)} のいずれか")
        if not str(e.get("label") or "").strip():
            errs.append(f"{where} に label（矢印の説明）がない")
        elif len(e["label"]) > LABEL_WARN_LEN:
            warns.append(f"{where} の label が長い（{len(e['label'])}字。{LABEL_WARN_LEN}字以内を推奨。詳しい説明は note に書く）")
        if e.get("evidence_level") not in EVIDENCE:
            errs.append(f"{where} の evidence_level は {list(EVIDENCE)} のいずれか")
        if e.get("evidence_level") == "hypothesis" and not str(e.get("note") or "").strip():
            errs.append(f"{where} は仮説（hypothesis）なので、推測の根拠を note に書く")
        check_src(where, e.get("source"))
        if e.get("group") and e["group"] not in gids:
            errs.append(f"{where} の group「{e['group']}」が groups に定義されていない")
        key = (e.get("from"), e.get("to"), e.get("type"), e.get("label"))
        if key in seen:
            errs.append(f"{where} が重複している（同じ主体・向き・種類・説明）")
        seen.add(key)
    used = {e.get("from") for e in edges} | {e.get("to") for e in edges}
    for nid, n in ids.items():
        if nid not in used:
            warns.append(f"主体「{n.get('name')}」に関係（矢印）がない")
    # 役割と矢印の向きの整合（明らかな向きの誤りを見つけるための警告）
    self_id = selfs[0]["id"] if len(selfs) == 1 else None
    for nid, n in ids.items():
        rel = {(e["type"], "out" if e["from"] == nid else "in") for e in edges if self_id in (e.get("from"), e.get("to"))
               and nid in (e.get("from"), e.get("to"))}
        if n.get("role") == "customer" and ("money", "in") in rel and ("money", "out") not in rel and ("service", "in") not in rel:
            warns.append(f"顧客「{n.get('name')}」が当社からお金を受け取るだけになっている（矢印の向きか role を確認）")
        if n.get("role") == "supplier" and ("money", "out") in rel and ("money", "in") not in rel and ("service", "out") not in rel:
            warns.append(f"仕入先・供給側「{n.get('name')}」が当社にお金を払うだけになっている（矢印の向きか role を確認）")
    plan, perrs, pwarns = ([], [], [])
    if not errs:
        plan, perrs, pwarns = plan_figures(bm)
        errs += perrs
        warns += pwarns
    return {"ok": not errs, "errors": errs, "warnings": warns, "n_nodes": len(nodes), "n_edges": len(edges),
            "figures": [{"figure": f["figure"], "title": f["title"], "nodes": len(f["nodes"]), "edges": len(f["edges"])}
                        for f in plan]}


# ============================================================================ 図の分割

def plan_figures(bm: dict) -> tuple[list[dict], list[str], list[str]]:
    """1枚に収まれば全社概要図（BM）のみ。上限を超えたら全社概要図と事業別（groups）の詳細図に分ける。"""
    nodes = {n["id"]: n for n in bm["nodes"]}
    edges = edges_of(bm)
    split = (bm.get("layout") or {}).get("split", "auto")
    over = len(nodes) > MAX_NODES or len(edges) > MAX_EDGES
    self_id = next(n["id"] for n in bm["nodes"] if n["role"] == "self")
    errs, warns = [], []

    def fig(fid, title, eds, extra_nodes=()):
        ids = []
        for e in eds:
            for k in ("from", "to"):
                if e[k] not in ids:
                    ids.append(e[k])
        for x in extra_nodes:
            if x not in ids:
                ids.append(x)
        order = [n for n in nodes if n in ids]   # JSON の順
        return {"figure": fid, "title": title, "nodes": order, "edges": [e["num"] for e in eds]}

    if split == "never" or (split == "auto" and not over):
        if over:
            warns.append(f"主体 {len(nodes)}・関係 {len(edges)} を1枚に描く（layout.split=never）。重なりが出たら分割すること")
        return [fig("BM", "全社概要", edges, list(nodes))], errs, warns
    groups = bm.get("groups") or []
    if not groups:
        errs.append(f"主体 {len(nodes)}・関係 {len(edges)} は1枚の上限（主体 {MAX_NODES}・関係 {MAX_EDGES}）を超える。"
                    "groups（事業）を定義し、各関係に group を付けて事業別の図に分けること")
        return [], errs, warns
    ov_edges = [e for e in edges if not e.get("group") or e.get("overview")]
    ov_extra = [self_id] + [n["id"] for n in bm["nodes"] if n.get("overview")]
    figs = [fig("BM", "全社概要", ov_edges, ov_extra)]
    if not ov_edges:
        errs.append("全社概要図に入る関係がない。全社に共通する関係は group を付けないか、overview: true を付けること")
    for g in groups:
        g_edges = [e for e in edges if e.get("group") == g["id"]]
        if not g_edges:
            warns.append(f"事業「{g['name']}」に関係がないため詳細図を作らない")
            continue
        g_nodes = [n["id"] for n in bm["nodes"] if g["id"] in (n.get("groups") or [])]
        figs.append(fig(f"BM-{g['id']}", g["name"], g_edges, g_nodes))
    for f in figs:
        if len(f["nodes"]) > MAX_NODES or len(f["edges"]) > MAX_EDGES:
            errs.append(f"図「{f['title']}」の主体 {len(f['nodes'])}・関係 {len(f['edges'])} が上限"
                        f"（主体 {MAX_NODES}・関係 {MAX_EDGES}）を超える。groups をさらに分けること")
    in_fig = {n for f in figs for n in f["nodes"]}
    for nid, n in nodes.items():
        if nid not in in_fig:
            warns.append(f"主体「{n['name']}」がどの図にも入らない（関係がないか、groups が未設定）")
    return figs, errs, warns


# ============================================================================ レイアウト

NODE_W = 116
ICON = 34
PAD_T, PAD_B = 6, 7
NAME_FONT, SELF_FONT, LABEL_FONT = 12, 13, 11
M = 24                      # 左右の余白
BASE_W = 820                # 図の基本の幅（他の図と同じ）
LABEL_W = 132               # 矢印の説明の折り返し幅
PORT_SP = 22                # 1つの辺に並ぶ矢印の端点の間隔
NODE_PAD = 6                # 矢印が主体を通過したとみなす余白


def text_w(s: str) -> float:
    """表示幅（全角＝1.0）。丸数字・矢印は日本語フォントでは全角幅で描かれるため 1.0 とする。"""
    w = 0.0
    for ch in s:
        o = ord(ch)
        w += 1.0 if (0x2460 <= o <= 0x24FF or 0x2190 <= o <= 0x21FF or 0x3251 <= o <= 0x32BF) else wide_len(ch)
    return w


def circ(n: int) -> str:
    if 1 <= n <= 20:
        return chr(0x2460 + n - 1)
    if 21 <= n <= 35:
        return chr(0x3251 + n - 21)
    if 36 <= n <= 50:
        return chr(0x32B1 + n - 36)
    return f"({n})"


class Box:
    def __init__(self, n: dict):
        self.id, self.n, self.role = n["id"], n, n["role"]
        font = SELF_FONT if self.role == "self" else NAME_FONT
        self.font = font
        self.name = wrap(n["name"], NODE_W - 8, font)
        self.w = NODE_W
        self.h = PAD_T + 5 + ICON + 4 + text_h(self.name, font) + PAD_B
        self.x = self.y = 0.0
        self.row = "top" if self.role == "investor" else ("bottom" if self.role in ("partner", "other") else "main")
        self.col = 0

    @property
    def cx(self):
        return self.x + self.w / 2

    @property
    def cy(self):
        return self.y + self.h / 2

    def rect(self, pad=0.0):
        return (self.x - pad, self.y - pad, self.w + 2 * pad, self.h + 2 * pad)


class Dummy:
    """2列以上離れた主体の間の矢印が途中の列を通る場所（図には描かない）。並び順と縦位置の計算に加える。"""
    def __init__(self, did: str, col: int, h: float):
        self.id, self.col, self.h, self.w = did, col, h, NODE_W
        self.role, self.row, self.n = "dummy", "main", {"name": ""}
        self.x = self.y = 0.0

    @property
    def cx(self):
        return self.x + self.w / 2

    @property
    def cy(self):
        return self.y + self.h / 2


def assign_columns(boxes: dict, eds: list[dict], model: str) -> None:
    """役割から列を決める（左＝供給側、中央＝当社とグループ、右＝販売チャネル・顧客・その先の利用者）。
    同じ側の主体の間に商品・サービスの流れがあれば、流れの向きに1列ずらす（段数は型により2〜3段まで）。"""
    tiers = 3 if model in CHAIN else 2
    chan = {b.id for b in boxes.values() if b.role == "channel"}
    adj = {}
    for e in eds:
        adj.setdefault(e["from"], set()).add(e["to"])
        adj.setdefault(e["to"], set()).add(e["from"])
    for b in boxes.values():
        b.col = {"self": 0, "group": 0, "supplier": -1, "channel": 1}.get(b.role, 0)
        if b.role == "customer":
            b.col = 2 if (adj.get(b.id, set()) & chan) else 1
    # 当社が運営・所有する事業（role: group）が2つ以上あり、それぞれが当社と顧客・販売チャネルだけと取引している場合は、
    # 事業を当社と顧客の間の列に置き、右側の列を1つずらす（当社 → 事業 → 顧客の順に並べ、同じ列の中の迂回をなくす）
    self_id = next((b.id for b in boxes.values() if b.role == "self"), None)
    right = {"channel", "customer", "end_user"}
    biz = [b for b in boxes.values() if b.role == "group" and b.row == "main"
           and any(e["from"] == self_id and e["to"] == b.id and e["type"] in ("operation", "ownership") for e in eds)
           and any(boxes[x].role in right for x in adj.get(b.id, ()) if x in boxes)
           and all(x == self_id or boxes[x].role in right for x in adj.get(b.id, ()) if x in boxes)]
    if len(biz) >= 2:
        for b in boxes.values():
            if b.role in ("channel", "customer"):
                b.col += 1
        for b in biz:
            b.col = 1
    cust_cols = [b.col for b in boxes.values() if b.role == "customer"] or [2 if chan else 1]
    for b in boxes.values():
        if b.role == "end_user":
            nb = [boxes[x].col for x in adj.get(b.id, ()) if x in boxes and boxes[x].role == "customer"]
            b.col = (max(nb) if nb else max(cust_cols)) + 1
    for _ in range(4):
        moved = False
        for e in eds:
            if e["type"] != "service":
                continue
            a, c = boxes[e["from"]], boxes[e["to"]]
            if a.row != "main" or c.row != "main" or a.role in ("self", "group") or c.role in ("self", "group"):
                continue
            if a.col < 0 and c.col < 0 and a.col >= c.col and c.col - 1 >= -tiers:
                a.col = c.col - 1
                moved = True
            elif a.col > 0 and c.col > 0 and c.col <= a.col and a.col + 1 <= tiers + 1:
                c.col = a.col + 1
                moved = True
        if not moved:
            break


def pava(desired: list[float], weights: list[float], gaps: list[float]) -> list[float]:
    """順序を保ち、隣どうしの間隔 gaps 以上を満たしつつ desired に最も近い位置（重み付き最小二乗）。"""
    off = [0.0]
    for g in gaps:
        off.append(off[-1] + g)
    z = [d - o for d, o in zip(desired, off)]
    blocks: list[list[float]] = []
    for zi, wi in zip(z, weights):
        blocks.append([zi * wi, wi, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, w, c = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += w
            blocks[-1][2] += c
    fit = []
    for s, w, c in blocks:
        fit += [s / w] * int(c)
    return [f + o for f, o in zip(fit, off)]


def seg_hits_rect(p, q, r) -> bool:
    """線分 pq が矩形 r（x, y, w, h）の内部を通るか（Liang–Barsky）。"""
    x0, y0 = p
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for pp, qq in ((-dx, x0 - r[0]), (dx, r[0] + r[2] - x0), (-dy, y0 - r[1]), (dy, r[1] + r[3] - y0)):
        if abs(pp) < 1e-12:
            if qq <= 0:
                return False
        else:
            t = qq / pp
            if pp < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 >= t1:
                return False
    return t1 - t0 > 1e-9


def rects_overlap(a, b, pad=0.0) -> bool:
    return (a[0] < b[0] + b[2] + pad and b[0] < a[0] + a[2] + pad and
            a[1] < b[1] + b[3] + pad and b[1] < a[1] + a[3] + pad)


def segs_cross(p1, p2, p3, p4) -> bool:
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    return (d1 * d2 < -1e-9) and (d3 * d4 < -1e-9)


class Layout:
    def __init__(self, bm: dict, fig: dict, col_step: float, row_gap: float, band_gap: float):
        self.bm, self.fig = bm, fig
        self.model = bm.get("model_type", "other")
        nodes = {n["id"]: n for n in bm["nodes"]}
        self.boxes = {nid: Box(nodes[nid]) for nid in fig["nodes"]}
        all_e = {e["num"]: e for e in edges_of(bm)}
        self.eds = [all_e[k] for k in fig["edges"]]
        self.col_step, self.row_gap, self.band_gap = col_step, row_gap, band_gap
        self.problems: list[str] = []

    # ---------------------------------------------------------------- 主体の配置
    def place_nodes(self, top: float) -> None:
        boxes = self.boxes
        assign_columns(boxes, self.eds, self.model)
        # 2列以上離れた主体の対には、途中の各列にダミーを置き、矢印の束が通る場所を確保する
        self.dummies, self.chains = {}, {}
        bundles: dict[tuple, list] = {}
        for e in self.eds:
            a, c = boxes[e["from"]], boxes[e["to"]]
            if a.row == "main" and c.row == "main" and abs(a.col - c.col) >= 2:
                key = tuple(sorted((a.id, c.id), key=lambda k: boxes[k].col))
                bundles.setdefault(key, []).append(e)
        for key, lst in bundles.items():
            chain = []
            for c in range(boxes[key[0]].col + 1, boxes[key[1]].col):
                d = Dummy(f"_d{len(self.dummies)}", c, 16 + PORT_SP * (len(lst) - 1))
                self.dummies[d.id] = d
                chain.append(d)
            self.chains[key] = chain
        B = {**boxes, **self.dummies}
        self.allb = B
        nb: dict[str, list[str]] = {k: [] for k in B}
        for e in self.eds:
            key = tuple(sorted((e["from"], e["to"]), key=lambda k: boxes[k].col))
            seq = [key[0]] + [d.id for d in self.chains.get(key, [])] + [key[1]]
            for u, v in zip(seq, seq[1:]):
                nb[u].append(v)
                nb[v].append(u)
        main = [b for b in B.values() if b.row == "main"]
        bands = {band: [b for b in B.values() if b.row == band] for band in ("top", "bottom")}
        cols = sorted({b.col for b in main})
        idx = {c: i for i, c in enumerate(cols)}
        n = len(cols)
        step = self.col_step if n <= 1 else max(self.col_step, (BASE_W - 2 * M - NODE_W) / (n - 1))
        width = BASE_W if n <= 1 else max(BASE_W, 2 * M + NODE_W + (n - 1) * step)
        for band, row in bands.items():
            if row:
                width = max(width, 2 * M + len(row) * NODE_W + (len(row) - 1) * 36)
        self.width = width
        span = (n - 1) * step + NODE_W if n > 1 else NODE_W
        x0 = (width - span) / 2
        for b in main:
            b.x = x0 + (idx[b.col] * step if n > 1 else 0)
        # 列の中の並びと縦位置：隣接する主体の縦位置の平均に近づける（交差を減らす）。当社は列の中央に固定
        by_col = {c: [b for b in main if b.col == c] for c in cols}
        for c in cols:
            y = 0.0
            for b in by_col[c]:
                b.y = y
                y += b.h + self.row_gap
            sh = (y - self.row_gap) / 2
            for b in by_col[c]:
                b.y -= sh
        self_b = next((b for b in main if b.role == "self"), None)

        band_pull = {b.id: (-400.0 if any(B[x].row == "top" for x in nb[b.id]) else 0.0) +
                     (400.0 if any(B[x].row == "bottom" for x in nb[b.id]) else 0.0) for b in main}

        def desired_y(b, c):
            ys = [B[x].cy for x in nb[b.id] if B[x].row == "main" and B[x].col != c]
            base = sum(ys) / len(ys) if ys else b.cy
            return base + band_pull[b.id] * (0.25 if ys else 1.0) if band_pull[b.id] else base

        order = sorted(cols, key=lambda c: abs(c))
        for _ in range(8):
            for c in order + order[::-1]:
                col = by_col[c]
                des = [0.0 if b is self_b else desired_y(b, c) for b in col]
                pairs = sorted(zip(des, range(len(col))), key=lambda t: (t[0], t[1]))
                col[:] = [col[i] for _, i in pairs]
                if self_b in col:
                    # 当社を列の中央に置き、グループ会社は上下に振り分ける
                    others = [b for b in col if b is not self_b]
                    mid = len(others) // 2
                    if band_pull[self_b.id] < 0:
                        mid = 0
                    elif band_pull[self_b.id] > 0:
                        mid = len(others)
                    col[:] = others[:mid] + [self_b] + others[mid:]
                d2 = [0.0 if b is self_b else desired_y(b, c) for b in col]
                w = [1000.0 if b is self_b else 1.0 for b in col]
                gaps = [(col[i].h + col[i + 1].h) / 2 + self.row_gap for i in range(len(col) - 1)]
                for b, cy in zip(col, pava(d2, w, gaps)):
                    b.y = cy - b.h / 2
        # 上段（株主など）と下段（提携先など）：つながる主体の横位置の平均に近づける
        for band, row in bands.items():
            if not row:
                continue
            des = []
            for b in row:
                xs = [B[x].cx for x in nb[b.id] if B[x].row != band]
                des.append(sum(xs) / len(xs) if xs else width / 2)
            pairs = sorted(zip(des, range(len(row))))
            row[:] = [row[i] for _, i in pairs]
            xs = pava([d for d, _ in pairs], [1.0] * len(row), [NODE_W + 36.0] * (len(row) - 1))
            lo, hi = M + NODE_W / 2, width - M - NODE_W / 2
            shift = max(0.0, lo - xs[0]) - max(0.0, xs[-1] - hi)
            for b, cx in zip(row, xs):
                b.x = cx + shift - NODE_W / 2
        # 縦方向の確定（上段 → 主要部 → 下段）
        y = top
        if bands["top"]:
            hmax = max(b.h for b in bands["top"])
            for b in bands["top"]:
                b.y = y + (hmax - b.h) / 2
            y += hmax + self.band_gap
        if main:
            mn = min(b.y for b in main)
            for b in main:
                b.y += y - mn
            y = max(b.y + b.h for b in main)
        if bands["bottom"]:
            y += self.band_gap
            hmax = max(b.h for b in bands["bottom"])
            for b in bands["bottom"]:
                b.y = y + (hmax - b.h) / 2
            y += hmax
        self.bottom = y

    # ---------------------------------------------------------------- 矢印の端点
    def side_toward(self, a: Box, b: Box) -> str:
        if a.row != b.row and "top" in (a.row, b.row):
            return "bottom" if a.row == "top" else "top"
        if a.row != b.row and "bottom" in (a.row, b.row):
            return "top" if a.row == "bottom" else "bottom"
        if a.row in ("top", "bottom"):
            return "right" if b.cx > a.cx else "left"
        if a.col == b.col:
            lo, hi = sorted((a.cy, b.cy))
            mids = [x for x in self.allb.values()
                    if x.row == "main" and x.col == a.col and x.id not in (a.id, b.id) and lo < x.cy < hi]
            if mids:
                # 間にある主体の左右の辺から出る矢印と交差しにくい側を選ぶ
                load = self.side_load

                def cost(side):
                    return (sum(load.get((x.id, side), 0) for x in mids) * 10 +
                            load.get((a.id, side), 0) + load.get((b.id, side), 0))
                return "right" if cost("right") <= cost("left") else "left"
            return "bottom" if b.cy > a.cy else "top"
        return "right" if b.col > a.col else "left"

    def assign_ports(self) -> None:
        B = self.boxes
        self.side_load = {}
        for e in self.eds:
            for me, other in ((e["from"], e["to"]), (e["to"], e["from"])):
                a, b = B[me], B[other]
                if a.row == "main" and b.row == "main" and a.col != b.col:
                    k = (me, "right" if b.col > a.col else "left")
                    self.side_load[k] = self.side_load.get(k, 0) + 1
        slots: dict[tuple[str, str], list] = {}
        pair_rank = {}
        pairs: dict[tuple, list] = {}
        for e in self.eds:
            pairs.setdefault(tuple(sorted((e["from"], e["to"]))), []).append(e)
        for key, lst in pairs.items():
            lst.sort(key=lambda e: (EDGE_ORDER.index(e["type"]), 0 if e["from"] == key[0] else 1, e["num"]))
            for i, e in enumerate(lst):
                pair_rank[e["num"]] = i
        for e in self.eds:
            key = tuple(sorted((e["from"], e["to"]), key=lambda k: B[k].col))
            chain = self.chains.get(key, [])
            for end, me, other in (("src", e["from"], e["to"]), ("dst", e["to"], e["from"])):
                a, b = B[me], B[other]
                side = self.side_toward(a, b)
                hop = (chain[0] if me == key[0] else chain[-1]) if chain else b
                slots.setdefault((me, side), []).append((e, end, hop))
        self.ports: dict[tuple[int, str], tuple[float, float, str]] = {}
        for (nid, side), lst in slots.items():
            a = B[nid]
            if side in ("left", "right"):
                def key(t, a=a):
                    e, _, o = t
                    if o.row == "main" and a.row == "main" and o.col == a.col and o.id != a.id:
                        # 同じ列の主体への迂回：上向きは辺の上端（近い相手ほど上）、下向きは下端（遠い相手ほど上）に置き、
                        # 迂回の経路どうし・横向きの矢印と交差しないようにする
                        dist = abs(o.cy - a.cy)
                        return (0, dist, 0.0, "", 0) if o.cy < a.cy else (2, -dist, 0.0, "", 0)
                    return (1, round(o.cy, 1), round(o.cx, 1), o.id, pair_rank[e["num"]])
                lst.sort(key=key)
                span, base = a.h, a.y
            else:
                lst.sort(key=lambda t: (round(t[2].cx, 1), round(t[2].cy, 1), t[2].id, pair_rank[t[0]["num"]]))
                span, base = a.w, a.x
            k = len(lst)
            sp = min(PORT_SP, (span - 20) / max(k - 1, 1)) if k > 1 else 0
            for i, (e, end, _) in enumerate(lst):
                off = base + span / 2 + (i - (k - 1) / 2) * sp
                if side == "left":
                    pt = (a.x, off)
                elif side == "right":
                    pt = (a.x + a.w, off)
                elif side == "top":
                    pt = (off, a.y)
                else:
                    pt = (off, a.y + a.h)
                self.ports[(e["num"], end)] = (pt[0], pt[1], side)

    # ---------------------------------------------------------------- 経路
    def obstacles(self, exclude=()) -> list[tuple]:
        return [b.rect(NODE_PAD) for k, b in self.boxes.items() if k not in exclude]

    def route_all(self) -> None:
        self.paths: dict[int, list[tuple[float, float]]] = {}
        used: list[tuple] = []
        pending = []
        ranks = {}
        bundles: dict[tuple, list] = {}
        for e in self.eds:
            bundles.setdefault(tuple(sorted((e["from"], e["to"]))), []).append(e)
        for key, lst in bundles.items():
            lst.sort(key=lambda e: (EDGE_ORDER.index(e["type"]), 0 if e["from"] == key[0] else 1, e["num"]))
            for i, e in enumerate(lst):
                ranks[e["num"]] = (i, len(lst))
        for e in self.eds:
            p = self.ports[(e["num"], "src")]
            q = self.ports[(e["num"], "dst")]
            key = tuple(sorted((e["from"], e["to"]), key=lambda k: self.boxes[k].col))
            chain = self.chains.get(key, [])
            obs = self.obstacles(exclude=(e["from"], e["to"]))
            if chain:
                i, k = ranks[e["num"]]
                off = (i - (k - 1) / 2) * PORT_SP
                mids = []
                for d in chain:
                    mids += [(d.x, d.cy + off), (d.x + d.w, d.cy + off)]
                if e["from"] != key[0]:
                    mids = mids[::-1]
                path = [(p[0], p[1])] + mids + [(q[0], q[1])]
                if not any(seg_hits_rect(u, v, r) for u, v in zip(path, path[1:]) for r in obs):
                    self.paths[e["num"]] = path
                    used += list(zip(path, path[1:]))
                    continue
                pending.append(e)
                continue
            straight = [(p[0], p[1]), (q[0], q[1])]
            blocked = any(seg_hits_rect(straight[0], straight[1], r) for r in obs)
            # 自分の始点・終点の主体の内側を通る場合も迂回する
            for nid, pt in ((e["from"], p), (e["to"], q)):
                r = self.boxes[nid].rect(-2)
                if seg_hits_rect(straight[0], straight[1], r):
                    blocked = True
            if blocked or (self.model in TWO_SIDED and self._crosses_self(straight, e)):
                pending.append(e)
            else:
                self.paths[e["num"]] = straight
                used.append((straight[0], straight[1]))
        for e in pending:
            path = self.route_grid(e, used)
            if path is None:
                self.problems.append(f"関係 {e['num']} の経路が見つからない")
                p, q = self.ports[(e["num"], "src")], self.ports[(e["num"], "dst")]
                path = [(p[0], p[1]), (q[0], q[1])]
            self.paths[e["num"]] = path
            for a, b in zip(path, path[1:]):
                used.append((a, b))

    def _crosses_self(self, straight, e) -> bool:
        """両面型のモデルで、供給側と需要側の直接の取引の線が当社の主体を通る場合は迂回させる。"""
        s = next((b for b in self.boxes.values() if b.role == "self"), None)
        if not s or s.id in (e["from"], e["to"]):
            return False
        return seg_hits_rect(straight[0], straight[1], s.rect(NODE_PAD + 4))

    def route_grid(self, e: dict, used: list) -> list | None:
        """直交格子上の最短経路（曲がり・既存の線との重なりに罰点）。主体（余白込み）を通らない。"""
        p = self.ports[(e["num"], "src")]
        q = self.ports[(e["num"], "dst")]
        stub = 14.0
        d = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}
        s0 = (p[0] + d[p[2]][0] * stub, p[1] + d[p[2]][1] * stub)
        s1 = (q[0] + d[q[2]][0] * stub, q[1] + d[q[2]][1] * stub)
        obs = [b.rect(NODE_PAD + 2) for b in self.boxes.values()]
        lo_x, hi_x = 6.0, self.width - 6.0
        xs = {s0[0], s1[0], lo_x + 4, hi_x - 4}
        ys = {s0[1], s1[1], self.top_limit + 2, self.bottom + 40}
        rects = [b.rect() for b in self.boxes.values()]
        for r in rects:
            xs |= {r[0] - 16, r[0] + r[2] + 16}
            ys |= {r[1] - 16, r[1] + r[3] + 16}
        sx = sorted({r[0] for r in rects} | {r[0] + r[2] for r in rects})
        sy = sorted({r[1] for r in rects} | {r[1] + r[3] for r in rects})
        xs |= {(a + b) / 2 for a, b in zip(sx, sx[1:])}
        ys |= {(a + b) / 2 for a, b in zip(sy, sy[1:])}
        xs = sorted(x for x in xs if lo_x <= x <= hi_x)
        ys = sorted(y for y in ys if y >= self.top_limit)

        def free_pt(pt):
            return not any(r[0] < pt[0] < r[0] + r[2] and r[1] < pt[1] < r[1] + r[3] for r in obs)

        def free_seg(a, b):
            return not any(seg_hits_rect(a, b, r) for r in obs)

        def overlap_cost(a, b):
            c = 0.0
            for (u, v) in used:
                if abs(a[1] - b[1]) < 1e-6 and abs(u[1] - v[1]) < 1e-6 and abs(a[1] - u[1]) < 4:
                    lo, hi = max(min(a[0], b[0]), min(u[0], v[0])), min(max(a[0], b[0]), max(u[0], v[0]))
                    c += max(0.0, hi - lo) * 6
                elif abs(a[0] - b[0]) < 1e-6 and abs(u[0] - v[0]) < 1e-6 and abs(a[0] - u[0]) < 4:
                    lo, hi = max(min(a[1], b[1]), min(u[1], v[1])), min(max(a[1], b[1]), max(u[1], v[1]))
                    c += max(0.0, hi - lo) * 6
                elif segs_cross(a, b, u, v):
                    c += 25
            return c

        import heapq
        xi = {x: i for i, x in enumerate(xs)}
        yi = {y: i for i, y in enumerate(ys)}
        if s0[0] not in xi or s0[1] not in yi or s1[0] not in xi or s1[1] not in yi:
            return None
        if not free_pt(s0) or not free_pt(s1):
            return None
        start = (xi[s0[0]], yi[s0[1]])
        goal = (xi[s1[0]], yi[s1[1]])
        sdir = d[p[2]]
        pq = [(0.0, start, sdir)]
        best = {(start, sdir): 0.0}
        prev = {}
        dirs = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        found = None
        while pq:
            cost, cur, cd = heapq.heappop(pq)
            if best.get((cur, cd), 1e18) < cost:
                continue
            if cur == goal:
                found = (cur, cd)
                break
            for nd in dirs:
                if nd[0] == -cd[0] and nd[1] == -cd[1]:
                    continue
                nx, ny = cur[0] + nd[0], cur[1] + nd[1]
                if not (0 <= nx < len(xs) and 0 <= ny < len(ys)):
                    continue
                a = (xs[cur[0]], ys[cur[1]])
                b = (xs[nx], ys[ny])
                if not free_pt(b) or not free_seg(a, b):
                    continue
                c = cost + math.dist(a, b) + (40 if nd != cd else 0) + overlap_cost(a, b)
                if (nx, ny) == goal:
                    # 終点の手前で、終点の辺に向かう向きでない場合は曲がりを1つ足す
                    gdir = (-d[q[2]][0], -d[q[2]][1])
                    c += 0 if nd == gdir else 40
                if c < best.get(((nx, ny), nd), 1e18):
                    best[((nx, ny), nd)] = c
                    prev[((nx, ny), nd)] = (cur, cd)
                    heapq.heappush(pq, (c, (nx, ny), nd))
        if not found:
            return None
        pts = []
        st = found
        while True:
            pts.append((xs[st[0][0]], ys[st[0][1]]))
            if st == (start, sdir):
                break
            st = prev[st]
        pts.reverse()
        path = [(p[0], p[1])] + pts + [(q[0], q[1])]
        return simplify(path)

    # ---------------------------------------------------------------- 矢印の説明の配置
    def label_text(self, e: dict) -> str:
        t = f"{circ(e['num'])} {e['label']}" + ("［仮説］" if e.get("evidence_level") == "hypothesis" else "")
        return wrap(t, LABEL_W, LABEL_FONT)

    def place_labels(self) -> None:
        node_rects = [b.rect(3) for b in self.boxes.values()]
        placed: list[tuple] = []
        self.labels: dict[int, dict] = {}
        all_segs = {num: list(zip(pts, pts[1:])) for num, pts in self.paths.items()}
        for e in self.eds:
            txt = self.label_text(e)
            lines = txt.split("\n")
            lw = max(text_w(s) for s in lines) * LABEL_FONT + 4
            lh = len(lines) * LABEL_FONT * LINE_H + 2
            pts = self.paths[e["num"]]
            segs = list(zip(pts, pts[1:]))
            lens = [math.dist(a, b) for a, b in segs]
            total = sum(lens) or 1.0
            cum = [0.0]
            for L in lens:
                cum.append(cum[-1] + L)
            best = None
            for si, ((a, b), L) in enumerate(zip(segs, lens)):
                if L < 18 and len(segs) > 1:
                    continue
                horiz = abs(b[1] - a[1]) < 1e-6
                vert = abs(b[0] - a[0]) < 1e-6
                nx, ny = (-(b[1] - a[1]) / L, (b[0] - a[0]) / L) if L else (0.0, -1.0)
                for t in (0.5, 0.38, 0.62, 0.27, 0.73, 0.17, 0.83):
                    px, py = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
                    s_al = cum[si] + L * t
                    pref = abs(s_al / total - 0.5)
                    # 線の上・線の両側・両側に少し離した位置（離すほど減点）
                    offs = [(0, 0, 0)]
                    for d in (0, 30, 60):
                        if horiz:
                            offs += [(0, -(lh / 2 + 3 + d / 2), d), (0, lh / 2 + 3 + d / 2, d)]
                        elif vert:
                            offs += [(lw / 2 + 4 + d, 0, d), (-(lw / 2 + 4 + d), 0, d)]
                        else:
                            k = (abs(nx) * lw + abs(ny) * lh) / 2 + 3 + d / 2
                            offs += [(nx * k, ny * k, d), (-nx * k, -ny * k, d)]
                    for oi, (ox, oy, d) in enumerate(offs):
                        cx, cy = px + ox, py + oy
                        r = (cx - lw / 2, cy - lh / 2, lw, lh)
                        hard = 0
                        if r[0] < 4 or r[0] + r[2] > self.width - 4 or r[1] < self.top_limit:
                            hard += 1
                        hard += sum(rects_overlap(r, nr) for nr in node_rects)
                        hard += sum(rects_overlap(r, pr, 2) for pr in placed)
                        # 他の矢印が説明の上を通ると文字が読みにくいため、大きく減点する
                        cross = [num for num, ss in all_segs.items()
                                 if num != e["num"] and any(seg_hits_rect(u, v, r) for u, v in ss)]
                        soft = 60.0 * len(cross)
                        if oi == 0 and not (horiz or vert):
                            soft += 2
                        if oi > 0:
                            soft += 8 + d * 0.8
                        score = (hard, soft + pref * 30 + (0 if L >= 40 else 15))
                        if best is None or score < best[0]:
                            best = (score, (cx, cy), r, s_al, total, (px, py), cross)
            (hard, _), (cx, cy), r, s_al, total, on_line, cross = best
            if hard:
                self.problems.append(f"関係 {e['num']} の説明を重ならない位置に置けない")
            for num in cross:
                self.label_cross.append(f"関係 {e['num']} の説明の上を関係 {num} の矢印が通っている")
            placed.append(r)
            self.labels[e["num"]] = {"text": txt, "lines": lines, "cx": cx, "cy": cy, "w": lw, "h": lh, "rect": r,
                                     "rel": 2 * s_al / total - 1, "offset": (cx - on_line[0], cy - on_line[1])}

    def run(self, top: float) -> None:
        self.top_limit = top - 8
        self.label_cross: list[str] = []
        self.place_nodes(top)
        self.assign_ports()
        self.route_all()
        self.place_labels()
        self.check()

    # ---------------------------------------------------------------- 書き出し前の幾何検査
    def check(self) -> None:
        bs = list(self.boxes.values())
        for i in range(len(bs)):
            for j in range(i + 1, len(bs)):
                if rects_overlap(bs[i].rect(), bs[j].rect(), 4):
                    self.problems.append(f"主体「{bs[i].n['name']}」と「{bs[j].n['name']}」が重なっている")
        for e in self.eds:
            pts = self.paths[e["num"]]
            for a, b in zip(pts, pts[1:]):
                for k, box in self.boxes.items():
                    pad = -2 if k in (e["from"], e["to"]) else NODE_PAD - 2
                    if seg_hits_rect(a, b, box.rect(pad)):
                        self.problems.append(f"関係 {e['num']} の矢印が主体「{box.n['name']}」を通過している")
                        break

    @property
    def content_bottom(self) -> float:
        ys = [self.bottom] + [y for pts in self.paths.values() for _, y in pts] + \
             [lb["rect"][1] + lb["rect"][3] for lb in self.labels.values()]
        return max(ys)


def simplify(path: list) -> list:
    out = [path[0]]
    for pt in path[1:]:
        if math.dist(pt, out[-1]) < 1e-6:
            continue
        if len(out) >= 2:
            a, b = out[-2], out[-1]
            if (abs(a[0] - b[0]) < 1e-6 and abs(b[0] - pt[0]) < 1e-6) or (abs(a[1] - b[1]) < 1e-6 and abs(b[1] - pt[1]) < 1e-6):
                out[-1] = pt
                continue
        out.append(pt)
    return out


# ============================================================================ 描画

def _attr(s: str) -> str:
    return escape(str(s), {'"': "&quot;"}).replace("\n", "&#xa;")


def node_style(b: Box) -> str:
    if b.role == "self":
        fill, stroke, sw, fc, fs, ic = "#E3F2FD", "#1565C0", 2.2, "#0D47A1", 1, "#1565C0"
    elif b.role == "group":
        fill, stroke, sw, fc, fs, ic = "#F4F8FB", "#78909C", 1.4, INK, 0, "#37474F"
    else:
        fill, stroke, sw, fc, fs, ic = "#FFFFFF", "#B0BEC5", 1.2, INK, 0, "#37474F"
    return (f"shape=label;html=0;whiteSpace=nowrap;rounded=1;arcSize=8;image={icon_data(b.n['type'], ic)};"
            f"imageWidth={ICON};imageHeight={ICON};imageAlign=center;imageVerticalAlign=top;spacing=0;spacingTop={PAD_T};"
            f"spacingBottom={PAD_B};spacingLeft=0;spacingRight=0;verticalAlign=bottom;align=center;fillColor={fill};"
            f"strokeColor={stroke};strokeWidth={sw};fontFamily={FONT};fontSize={b.font};fontColor={fc};fontStyle={fs};")


def edge_style(e: dict, sp, tp, sb: Box, tb: Box) -> str:
    _, color, dash = EDGE_TYPES[e["type"]]
    ex, ey = (sp[0] - sb.x) / sb.w, (sp[1] - sb.y) / sb.h
    nx, ny = (tp[0] - tb.x) / tb.w, (tp[1] - tb.y) / tb.h
    s = (f"endArrow=block;endFill=1;endSize=7;startArrow=none;html=0;rounded=0;edgeStyle=none;strokeColor={color};"
         f"strokeWidth=2;fontColor={color};fontFamily={FONT};fontSize={LABEL_FONT};labelBackgroundColor=#FFFFFF;"
         f"exitX={ex:.5f};exitY={ey:.5f};exitDx=0;exitDy=0;exitPerimeter=0;"
         f"entryX={nx:.5f};entryY={ny:.5f};entryDx=0;entryDy=0;entryPerimeter=0;")
    if dash:
        s += f"dashed=1;dashPattern={dash};"
    if e.get("evidence_level") == "hypothesis":
        s += "opacity=60;"
    return s


def fig_sources(bm: dict, data: dict | None, lay: Layout) -> list[str]:
    srcs, _ = merged_sources(bm, data)
    ids = []
    for b in lay.boxes.values():
        ids += src_ids(b.n.get("source"))
    for e in lay.eds:
        ids += src_ids(e.get("source"))
    out = []
    for i in dict.fromkeys(ids):
        s = srcs.get(i, {})
        out.append(f"{i} {s.get('title', '')}" + (f"（{s['published']}公表）" if s.get("published") else ""))
    return out


def render(bm: dict, data: dict | None, fig: dict, params: tuple) -> tuple[Canvas, Layout]:
    comp = bm["company"]
    cname = comp.get("short_name") or comp["name"]
    cv = Canvas(BASE_W, fig["figure"])
    title = f"{cname}　ビジネスモデル（{fig['title']}）"
    period = str(bm.get("period", "")).replace("_", " ")
    sub = f"{period}時点の資料による／型：{MODEL_TYPES.get(bm.get('model_type'), 'その他')}／矢印は資料で確認した取引関係"
    lay = Layout(bm, fig, *params)
    sub_w = wrap(sub, BASE_W - 48, 12)
    hdr_bottom = 46 + text_h(sub_w, 12) + (18 if bm.get("sample") else 0)
    lay.run(top=hdr_bottom + 30)
    W = lay.width
    cv.w = W
    cv.text(24, 16, W - 48, 26, title, font=18, bold=True)
    cv.text(24, 46, W - 48, text_h(sub_w, 12), sub_w, font=12, color=SUB)
    if bm.get("sample"):
        cv.text(24, 46 + text_h(sub_w, 12) + 3, W - 48, 14, "【サンプルデータ：実在企業の関係ではない】", font=11, color="#B71C1C")
    # 主体
    for b in lay.boxes.values():
        meta = {"fra_node": b.id, "fra_role": b.role, "fra_type": b.n["type"]}
        attrs = " ".join(f'{k}="{_attr(v)}"' for k, v in meta.items())
        cv.cells.append(f'<object id="n-{b.id}" label="{_attr(b.name)}" {attrs}><mxCell style="{node_style(b)}" vertex="1" parent="1">'
                        f'<mxGeometry x="{b.x:.2f}" y="{b.y:.2f}" width="{b.w:.2f}" height="{b.h:.2f}" as="geometry"/></mxCell></object>')
        lines = b.name.split("\n")
        tw = max(text_w(s) for s in lines) * b.font
        th = len(lines) * b.font * LINE_H
        cv.texts.append({"lines": lines, "x": b.cx - tw / 2, "y": b.y + b.h - PAD_B - th, "w": tw, "h": th, "font": b.font,
                         "container": b.rect(), "kind": "node"})
    # 矢印
    for e in lay.eds:
        sp, tp = lay.ports[(e["num"], "src")], lay.ports[(e["num"], "dst")]
        pts = lay.paths[e["num"]]
        lb = lay.labels[e["num"]]
        way = "".join(f'<mxPoint x="{x:.2f}" y="{y:.2f}"/>' for x, y in pts[1:-1])
        arr = f'<Array as="points">{way}</Array>' if way else ""
        meta = {"fra_edge": e["num"], "fra_from": e["from"], "fra_to": e["to"], "fra_type": e["type"],
                "fra_evidence": e["evidence_level"]}
        attrs = " ".join(f'{k}="{_attr(v)}"' for k, v in meta.items())
        cv.cells.append(
            f'<object id="e{e["num"]}" label="{_attr(lb["text"])}" {attrs}>'
            f'<mxCell style="{edge_style(e, sp, tp, lay.boxes[e["from"]], lay.boxes[e["to"]])}" edge="1" parent="1" '
            f'source="n-{e["from"]}" target="n-{e["to"]}"><mxGeometry x="{lb["rel"]:.5f}" relative="1" as="geometry">'
            f'<mxPoint x="{lb["offset"][0]:.2f}" y="{lb["offset"][1]:.2f}" as="offset"/>{arr}</mxGeometry></mxCell></object>')
        cv.texts.append({"lines": lb["lines"], "x": lb["cx"] - (lb["w"] - 4) / 2, "y": lb["cy"] - (lb["h"] - 2) / 2,
                         "w": lb["w"] - 4, "h": lb["h"] - 2, "font": LABEL_FONT, "container": None, "kind": "edge_label",
                         "edge": e["num"]})
    y = lay.content_bottom + 26
    # 凡例
    cv.line(24, y, W - 24, y, color=GRID)
    y += 10
    cv.text(24, y, 60, 16, "凡例", font=11, bold=True, color=SUB)
    x = 70
    used = [t for t in EDGE_ORDER if any(e["type"] == t for e in lay.eds)]
    items = [("edge", t) for t in used]
    if any(b.role == "self" for b in lay.boxes.values()):
        items.append(("self", None))
    if any(b.role == "group" for b in lay.boxes.values()):
        items.append(("group", None))
    if any(e.get("evidence_level") == "hypothesis" for e in lay.eds):
        items.append(("hyp", None))
    legend_names = []
    for kind, t in items:
        if kind == "edge":
            name, color, dash = EDGE_TYPES[t]
            label = name
            need = 34 + text_w(label) * 11 + 18
        elif kind in ("self", "group"):
            label = "＝分析対象の会社" if kind == "self" else "＝グループ会社・事業部門"
            need = 22 + text_w(label) * 11 + 18
        else:
            label = "［仮説］＝資料からの推測"
            need = text_w(label) * 11 + 18
        if x + need > W - 24:
            x = 70
            y += 20
        if kind == "edge":
            st = (f"endArrow=block;endFill=1;endSize=6;html=0;strokeColor={color};strokeWidth=2;" +
                  (f"dashed=1;dashPattern={dash};" if dash else ""))
            cv.cells.append(f'<mxCell id="{cv._id("g")}" value="" style="{st}" edge="1" parent="1"><mxGeometry relative="1" as="geometry">'
                            f'<mxPoint x="{x:.2f}" y="{y + 8:.2f}" as="sourcePoint"/><mxPoint x="{x + 28:.2f}" y="{y + 8:.2f}" as="targetPoint"/>'
                            f'</mxGeometry></mxCell>')
            cv.text(x + 34, y, text_w(label) * 11 + 4, 16, label, font=11, color=color)
        elif kind in ("self", "group"):
            fill, stroke = ("#E3F2FD", "#1565C0") if kind == "self" else ("#F4F8FB", "#78909C")
            cv.rect(x, y + 2, 16, 12, fill, stroke, stroke_width=1.6, track_text=False)
            cv.text(x + 22, y, text_w(label) * 11 + 4, 16, label, font=11, color=SUB)
        else:
            cv.text(x, y, text_w(label) * 11 + 4, 16, label, font=11, color=SUB)
        legend_names.append(label)
        x += need
    y += 26
    notes = ["出典：" + "／".join(fig_sources(bm, data, lay)),
             "主体と関係は出典の資料で確認したもの（根拠が「仮説」の関係を除く）。一般名称の主体は、資料に個別の相手先名が記載されていないもの。"]
    txt = wrap("\n".join(notes), W - 48, 11)
    cv.text(24, y, W - 48, text_h(txt, 11), txt, font=11, color=SUB)
    y += text_h(txt, 11) + 14
    cv.h = y
    cv.legend = legend_names
    return cv, lay


# ============================================================================ 検証（書き出し後）

def _edge_points(obj, cells: dict) -> list[tuple[float, float]]:
    c = obj.find("mxCell")
    st = dict(kv.split("=", 1) for kv in c.get("style", "").split(";") if "=" in kv)
    s, t = cells[c.get("source")], cells[c.get("target")]
    p0 = (s[0] + float(st["exitX"]) * s[2], s[1] + float(st["exitY"]) * s[3])
    p1 = (t[0] + float(st["entryX"]) * t[2], t[1] + float(st["entryY"]) * t[3])
    mids = [(float(p.get("x")), float(p.get("y"))) for p in c.findall("mxGeometry/Array/mxPoint")]
    return [p0] + mids + [p1]


def verify_bm(drawio_path: Path, bm: dict, fig: dict, manifest: dict | None = None) -> dict:
    """図の構造を JSON と照合する：主体と関係がすべてあるか、矢印の始点・終点の主体（向き）、
    主体の重なり、矢印が他の主体を通過しないか、矢印の説明が主体に重ならないか。文字の検査は diagrams.verify。"""
    errs, warns = [], []
    root = ET.parse(drawio_path).getroot()
    nodes, cells, edges = {}, {}, {}
    for obj in root.iter("object"):
        c = obj.find("mxCell")
        if obj.get("fra_node"):
            g = c.find("mxGeometry")
            r = (float(g.get("x")), float(g.get("y")), float(g.get("width")), float(g.get("height")))
            if obj.get("fra_node") in nodes:
                errs.append(f"主体「{obj.get('fra_node')}」が図に2回以上ある")
            nodes[obj.get("fra_node")] = (obj.get("id"), r)
            cells[obj.get("id")] = r
            style = c.get("style", "")
            img = re.search(r"image=([^;]+)", style)
            if not img or not img.group(1).startswith("data:image/svg+xml,"):
                errs.append(f"主体「{obj.get('fra_node')}」のアイコンが埋め込みの SVG ではない（外部画像は使わない）")
        elif obj.get("fra_edge"):
            edges[int(obj.get("fra_edge"))] = obj
    want_nodes = set(fig["nodes"])
    for nid in want_nodes - set(nodes):
        errs.append(f"主体「{nid}」が図にない")
    for nid in set(nodes) - want_nodes:
        errs.append(f"図に JSON にない主体「{nid}」がある")
    by_num = {e["num"]: e for e in edges_of(bm)}
    id_of = {nid: v[0] for nid, v in nodes.items()}
    for num in fig["edges"]:
        e = by_num[num]
        obj = edges.get(num)
        if obj is None:
            errs.append(f"関係 {num}（{e['label']}）の矢印が図にない")
            continue
        c = obj.find("mxCell")
        if c.get("source") != id_of.get(e["from"]) or c.get("target") != id_of.get(e["to"]):
            errs.append(f"関係 {num}（{e['label']}）の矢印の向きが JSON と異なる（{e['from']} → {e['to']} であるべき）")
        st = c.get("style", "")
        if "endArrow=block" not in st or "startArrow=none" not in st:
            errs.append(f"関係 {num} の矢印の向きの表示（矢じり）が終点側だけになっていない")
        if obj.get("fra_type") != e["type"] or f"strokeColor={EDGE_TYPES[e['type']][1]}" not in st:
            errs.append(f"関係 {num} の種類・色が JSON と異なる")
        if not (obj.get("label") or "").startswith(circ(num)):
            errs.append(f"関係 {num} の説明に番号 {circ(num)} がない")
    for num in set(edges) - set(fig["edges"]):
        errs.append(f"図に JSON にない関係 {num} がある")
    # 幾何
    items = list(nodes.items())
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if rects_overlap(items[i][1][1], items[j][1][1], 4):
                errs.append(f"主体「{items[i][0]}」と「{items[j][0]}」が重なっている")
    n_cross = 0
    polys = {}
    for num, obj in edges.items():
        if num not in by_num:
            continue
        try:
            pts = _edge_points(obj, cells)
        except (KeyError, ValueError):
            errs.append(f"関係 {num} の矢印が主体に接続されていない（draw.io で主体を動かすと矢印が外れる）")
            continue
        polys[num] = pts
        e = by_num[num]
        for a, b in zip(pts, pts[1:]):
            for nid, (_, r) in nodes.items():
                pad = -2 if nid in (e["from"], e["to"]) else NODE_PAD - 2
                rr = (r[0] - pad, r[1] - pad, r[2] + 2 * pad, r[3] + 2 * pad)
                if seg_hits_rect(a, b, rr):
                    errs.append(f"関係 {num} の矢印が主体「{nid}」を通過している")
    ks = sorted(polys)
    for i in range(len(ks)):
        for j in range(i + 1, len(ks)):
            for a, b in zip(polys[ks[i]], polys[ks[i]][1:]):
                for u, v in zip(polys[ks[j]], polys[ks[j]][1:]):
                    if segs_cross(a, b, u, v):
                        n_cross += 1
    if manifest:
        for t in manifest["texts"]:
            if t.get("kind") != "edge_label":
                continue
            r = (t["x"], t["y"], t["w"], t["h"])
            for nid, (_, nr) in nodes.items():
                if rects_overlap(r, nr, -1):
                    errs.append(f"関係 {t['edge']} の説明が主体「{nid}」に重なっている")
        texts = {ln for t in manifest["texts"] for ln in t["lines"]}
        for name in manifest.get("legend", []):
            if name not in texts:
                errs.append(f"凡例「{name}」がない")
        dup = [t["lines"] for t in manifest["texts"] if t.get("kind") == "edge_label"]
        if len({tuple(x) for x in dup}) != len(dup):
            errs.append("矢印の説明が重複している")
    if n_cross:
        warns.append(f"矢印の交差 {n_cross} か所")
    return {"ok": not errs, "errors": errs, "warnings": warns, "n_nodes": len(nodes), "n_edges": len(edges),
            "n_crossings": n_cross}


# ============================================================================ 生成

TRIES = [(212, 54, 70), (240, 76, 84), (270, 100, 100), (310, 130, 120)]
LABEL_CROSS_PX = 150


def build_figure(bm: dict, data: dict | None, fig: dict, out: Path, prefix: str, png: bool) -> dict:
    # 配置の問題（主体・説明の重なりなど）がない間隔を使う。説明の上を他の矢印が通る箇所が残る場合は、
    # 間隔を広げて減らすが、図が大きくなりすぎないように、重なり1か所を LABEL_CROSS_PX（px）と換算して比べる。
    tried = []
    for params in TRIES:
        cv, lay = render(bm, data, fig, params)
        tried.append((cv, lay, params))
        if not lay.problems and not lay.label_cross:
            break
    ok = [t for t in tried if not t[1].problems]
    # 幅が既定の幅を超えるとノートでは縮小表示され文字が小さくなるため、超えた分も加える
    cv, lay, params = min(ok, key=lambda t: t[0].h + max(0.0, t[0].w - BASE_W) + LABEL_CROSS_PX * len(t[1].label_cross)) \
        if ok else tried[-1]
    p = out / f"{prefix}_{fig['figure']}.drawio"
    p.write_text(cv.to_xml(), encoding="utf-8")
    svg = export(p, "svg")
    if png:
        export(p, "png", scale=1.5)
    manifest = {"width": cv.w, "height": cv.h, "texts": cv.texts, "segs": cv.segs, "legend": cv.legend}
    v1 = verify_texts(p, manifest)
    v2 = verify_bm(p, bm, fig, manifest)
    errs = v1["errors"] + v2["errors"] + [f"配置：{x}" for x in lay.problems if not any(x in y for y in v2["errors"])]
    return {"figure": fig["figure"], "title": fig["title"], "drawio": str(p), "svg": str(svg),
            "png": str(p.with_suffix(".png")) if png else None, "size": [cv.w, round(cv.h)],
            "nodes": fig["nodes"], "edges": fig["edges"], "layout_params": list(params),
            "verify": {"ok": not errs, "errors": errs, "warnings": v1["warnings"] + v2["warnings"] + lay.label_cross,
                       "n_crossings": v2["n_crossings"], "n_svg_texts": v1["n_svg_texts"]}}


def build(bm_path: str, out_dir: str, prefix: str | None = None, data_path: str | None = None, png: bool = False) -> dict:
    """図を作り、結果（失敗時は原因）を <prefix>_BM_figures.json に保存する。例外は投げない。"""
    bm_p = Path(bm_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    res = {"ok": False, "version": VERSION, "bizmodel": str(bm_p), "stage": None, "error": None, "figures": []}
    prefix_ = prefix
    try:
        res["stage"] = "load"
        bm = load_json(bm_p)
        prefix_ = prefix or bm.get("period") or bm_p.stem
        dp = Path(data_path) if data_path else default_data_path(bm_p)
        data = load_json(dp) if dp else None
        res["data"] = str(dp) if dp else None
        res["stage"] = "validate"
        v = validate(bm, data)
        res["validation"] = v
        if not v["ok"]:
            raise ValueError("ビジネスモデル JSON の検証エラー：" + "／".join(v["errors"][:5]))
        figs, _, _ = plan_figures(bm)
        res["stage"] = "build"
        old = out / f"{prefix_}_BM_figures.json"
        stale = []
        if old.exists():
            try:
                stale = [f["figure"] for f in json.loads(old.read_text(encoding="utf-8")).get("figures", [])]
            except (ValueError, KeyError):
                stale = []
        for f in figs:
            res["figures"].append(build_figure(bm, data, f, out, prefix_, png))
        # 前回の生成物のうち今回作らなかった図（分割の変更など）を削除する
        keep = {f["figure"] for f in figs}
        for fid in stale:
            if fid not in keep:
                for ext in (".drawio", ".svg", ".png"):
                    q = out / f"{prefix_}_{fid}{ext}"
                    if q.exists():
                        q.unlink()
        bad = [f for f in res["figures"] if not f["verify"]["ok"]]
        if bad:
            res["stage"] = "verify"
            raise ValueError("図の検証エラー：" + "／".join(f"{f['figure']}: {f['verify']['errors'][:3]}" for f in bad))
        res["ok"] = True
        res["stage"] = "done"
    except Exception as ex:  # noqa: BLE001 - 失敗の原因を記録し、財務分析は続行できるようにする
        res["error"] = f"{type(ex).__name__}: {ex}"
    if bm_p.exists():
        res["bizmodel_sha256"] = hashlib.sha256(bm_p.read_bytes()).hexdigest()
    if prefix_:
        res["record"] = str(out / f"{prefix_}_BM_figures.json")
        (out / f"{prefix_}_BM_figures.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


# ============================================================================ 表（ノート用）

def _cell(s) -> str:
    return str(s if s is not None else "").replace("|", "｜").replace("\n", " ")


def table(bm: dict) -> str:
    """ノートの「ビジネスモデル」に貼る主体の表（出典つき）。check_report.py が再生成して照合する。
    取引関係（矢印）ごとの内容・根拠・出典はビジネスモデル JSON に残し、ノートには載せない。"""
    figs, _, _ = plan_figures(bm)
    rows = ["<!-- fra:table:bizmodel -->", "**主体**", "", "| 主体 | 種類 | 役割 | 説明 | 出典 |", "|---|---|---|---|---|"]
    for n in bm["nodes"]:
        rows.append(f"| {_cell(n['name'])} | {NODE_TYPES[n['type']]} | {ROLES[n['role']]} | {_cell(n.get('description'))} | "
                    f"{_cell(n.get('source'))} |")
    if len(figs) > 1:
        rows += ["", "図の分割：" + "、".join(f["title"] for f in figs)]
    rows.append("<!-- /fra:table:bizmodel -->")
    return "\n".join(rows)


# ============================================================================ 下書き

def init(data_path: str, out_path: str) -> dict:
    d = load_json(data_path)
    out = Path(out_path)
    if out.exists():
        return {"ok": False, "action": "exists", "path": str(out), "message": "既に存在する。上書きせずに編集すること"}
    c = d["company"]
    bm = {
        "schema": SCHEMA, "schema_version": 1, "sample": bool(d.get("sample")), "draft": True,
        "company": {"name": c["name"], "short_name": c.get("short_name"), "code": c["code"]},
        "period": d["period"]["label"], "model_type": "other", "layout": {"split": "auto"},
        "sources": [], "groups": [],
        "nodes": [{"id": "self", "name": c.get("short_name") or c["name"], "type": "company", "role": "self",
                   "description": "", "source": ""}],
        "edges": [], "notes": [],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bm, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "action": "created", "path": str(out),
            "todo": ["model_type", "sources（有報・説明資料・企業サイトなど。財務データ JSON の S1 などはそのまま参照できる）",
                     "nodes（description・source）", "edges（type・label・evidence_level・source）", "draft: false"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    i = sp.add_parser("init")
    i.add_argument("--data", required=True)
    i.add_argument("--out", required=True)
    for c in ("validate", "table"):
        x = sp.add_parser(c)
        x.add_argument("bizmodel")
        x.add_argument("--data")
    b = sp.add_parser("build")
    b.add_argument("bizmodel")
    b.add_argument("--out-dir", required=True)
    b.add_argument("--prefix")
    b.add_argument("--data")
    b.add_argument("--png", action="store_true", help="目視確認用の PNG も書き出す")
    v = sp.add_parser("verify")
    v.add_argument("drawio")
    v.add_argument("--bizmodel", required=True)
    a = ap.parse_args()
    if a.cmd == "init":
        r = init(a.data, a.out)
        emit(r)
        sys.exit(0 if r["ok"] else 1)
    if a.cmd == "build":
        r = build(a.bizmodel, a.out_dir, a.prefix, a.data, a.png)
        emit(r)
        sys.exit(0 if r["ok"] else 1)
    bm_p = Path(a.bizmodel)
    bm = load_json(bm_p)
    if a.cmd == "verify":
        m = re.search(r"_(BM(?:-[a-z0-9_]+)?)$", Path(a.drawio).stem)
        fid = m.group(1) if m else ""
        figs, _, _ = plan_figures(bm)
        fig = next((f for f in figs if f["figure"] == fid), None)
        if fig is None:
            emit({"ok": False, "errors": [f"図 {fid} は JSON の分割計画にない"]})
            sys.exit(1)
        r = verify_bm(Path(a.drawio), bm, fig)
        emit(r)
        sys.exit(0 if r["ok"] else 1)
    dp = Path(a.data) if a.data else default_data_path(bm_p)
    data = load_json(dp) if dp else None
    if a.cmd == "validate":
        r = validate(bm, data)
        emit(r)
        sys.exit(0 if r["ok"] else 1)
    v = validate(bm, data)
    if not v["ok"]:
        emit(v)
        sys.exit(1)
    print(table(bm))


if __name__ == "__main__":
    main()
