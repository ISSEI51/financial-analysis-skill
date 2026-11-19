#!/usr/bin/env python3
"""決算データ JSON から財務三表の図（draw.io）を生成し、SVG に書き出して検証する。

- 図形の高さは金額に比例させる（同一パネル内で縮尺は1つ）。
- ラベルは draw.io の非 HTML ラベル（SVG の <text> 要素）で出力し、プログラムで照合できるようにする。
- 各図形には fra_value / fra_scale 属性を持たせ、書き出し後に「高さ = |金額| × 縮尺」を検証する。

使い方:
  python3 -I diagrams.py build  <data.json> --out-dir <assets_dir> [--prefix 2026年3月期_本決算] [--png]
  python3 -I diagrams.py verify <assets_dir>/<prefix>_PL.drawio [...]

出力: JSON（生成ファイル、図中の数値、検証結果）
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fra_common import VERSION, D, choose_display_unit, convert, fmt_amount, fmt_num, load_config, round_half_up, wide_len  # noqa: E402

FONT = "Hiragino Sans,Hiragino Kaku Gothic ProN,Noto Sans JP,Yu Gothic,Meiryo,sans-serif"
INK = "#1f2d3d"
SUB = "#5b6770"
GRID = "#c9d1d6"
# 配色（白背景・教科書調）
TEAL = ["#cfeeed", "#a9dedb", "#84ccc8", "#5fb9b4", "#bfe6e4", "#96d4d0"]   # 収益・資産
TEAL_STROKE = "#2f8f89"
ORANGE = ["#fde3c8", "#fbcb9a", "#f8b273", "#fcd8b0", "#f6a35c"]           # 費用・負債
ORANGE_STROKE = "#c26b1e"
DEBT = "#f08a3c"                                                            # 有利子負債
GREEN = ["#d3ecc9", "#b2dca4", "#93cc84"]                                   # 利益・純資産
GREEN_STROKE = "#3f8f3a"
NEG_FILL = "#fbd5d5"                                                        # 損失・マイナス
NEG_STROKE = "#c62828"
NEG_TEXT = "#b71c1c"
CASH = "#6fb7c9"
CASH_STROKE = "#2c7f94"
POS_FLOW = "#8cc97f"
POS_STROKE = "#3f8f3a"

LINE_H = 1.28  # 行の高さ ÷ フォントサイズ（draw.io の非HTMLラベルの行送り ≒ 1.2）


# ============================================================================ キャンバス

class Canvas:
    def __init__(self, width: int, name: str):
        self.w, self.h, self.name = width, 0, name
        self.cells: list[str] = []
        self.texts: list[dict] = []     # 検証用: 文字列と推定外接矩形
        self.blocks: list[dict] = []    # 検証用: 金額を表す図形
        self.segs: list[tuple] = []     # 検証用: テキストと重なってはいけない線（括弧・軸）
        self.n = 0

    def _id(self, p="c"):
        self.n += 1
        return f"{p}{self.n}"

    @staticmethod
    def _style(**kw):
        return ";".join(f"{k}={v}" for k, v in kw.items() if v is not None) + ";"

    def rect(self, x, y, w, h, fill, stroke, *, label="", font=12, bold=False, color=INK, dashed=False,
             stroke_width=1, meta=None, align="center", valign="middle", track_text=True, rounded=0):
        cid = self._id("b")
        style = self._style(rounded=rounded, whiteSpace="nowrap", html=0, fillColor=fill, strokeColor=stroke,
                            strokeWidth=stroke_width, dashed=1 if dashed else 0, fontFamily=FONT, fontSize=font,
                            fontColor=color, fontStyle=1 if bold else 0, align=align, verticalAlign=valign,
                            spacing=2)
        geo = f'<mxGeometry x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" as="geometry"/>'
        lab = escape(label).replace("\n", "&#xa;")
        if meta:
            attrs = " ".join(f"{k}={quoteattr(str(v))}" for k, v in meta.items())
            self.cells.append(f'<object id="{cid}" label="{lab}" {attrs}><mxCell style="{style}" vertex="1" parent="1">{geo}</mxCell></object>')
            self.blocks.append({"id": cid, "x": x, "y": y, "w": w, "h": h, **meta})
        else:
            self.cells.append(f'<mxCell id="{cid}" value="{lab}" style="{style}" vertex="1" parent="1">{geo}</mxCell>')
        if label and track_text:
            self._track(label, x, y, w, h, font, align, valign, container=(x, y, w, h))
        return cid

    def text(self, x, y, w, h, label, *, font=12, bold=False, color=INK, align="left", valign="top", bg=False):
        cid = self._id("t")
        style = self._style(text=None, html=0, whiteSpace="nowrap", fontFamily=FONT, fontSize=font, fontColor=color,
                            fontStyle=1 if bold else 0, align=align, verticalAlign=valign, spacing=0,
                            fillColor="none", strokeColor="none", labelBackgroundColor="#FFFFFF" if bg else None)
        style = "text;" + style
        lab = escape(label).replace("\n", "&#xa;")
        self.cells.append(f'<mxCell id="{cid}" value="{lab}" style="{style}" vertex="1" parent="1">'
                          f'<mxGeometry x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" as="geometry"/></mxCell>')
        self._track(label, x, y, w, h, font, align, valign)
        return cid

    def _track(self, label, x, y, w, h, font, align, valign, container=None):
        lines = label.split("\n")
        tw = max(wide_len(s) for s in lines) * font
        th = len(lines) * font * LINE_H
        tx = {"left": x, "center": x + (w - tw) / 2, "right": x + w - tw}[align]
        ty = {"top": y, "middle": y + (h - th) / 2, "bottom": y + h - th}[valign]
        self.texts.append({"lines": lines, "x": tx, "y": ty, "w": tw, "h": th, "font": font, "container": container})

    def line(self, x1, y1, x2, y2, *, color=SUB, width=1, dashed=False, arrow=False, check=False):
        cid = self._id("e")
        if check:
            self.segs.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
        style = self._style(endArrow="block" if arrow else "none", startArrow="none", html=0, strokeColor=color,
                            strokeWidth=width, dashed=1 if dashed else 0, endSize=5)
        self.cells.append(f'<mxCell id="{cid}" value="" style="{style}" edge="1" parent="1">'
                          f'<mxGeometry relative="1" as="geometry"><mxPoint x="{x1:.2f}" y="{y1:.2f}" as="sourcePoint"/>'
                          f'<mxPoint x="{x2:.2f}" y="{y2:.2f}" as="targetPoint"/></mxGeometry></mxCell>')

    def pie_slice(self, x, y, dia, start, end, fill, *, meta):
        """円グラフの扇形。start / end は円周に対する割合（0＝12時の位置、時計回り）。"""
        cid = self._id("p")
        end_s = min(end, 0.999999)   # 0〜1 をそのまま指定すると描画されないため
        style = self._style(shape="mxgraph.basic.pie", startAngle=f"{start:.6f}", endAngle=f"{end_s:.6f}", html=0,
                            fillColor=fill, strokeColor="#FFFFFF", strokeWidth=1.5)
        geo = f'<mxGeometry x="{x:.2f}" y="{y:.2f}" width="{dia:.2f}" height="{dia:.2f}" as="geometry"/>'
        attrs = " ".join(f"{k}={quoteattr(str(v))}" for k, v in meta.items())
        self.cells.append(f'<object id="{cid}" label="" {attrs}><mxCell style="{style}" vertex="1" parent="1">{geo}</mxCell></object>')
        self.blocks.append({"id": cid, "x": x, "y": y, "w": dia, "h": dia, **meta})
        return cid

    def bracket_v(self, x, y1, y2, side="right", color=SUB):
        """縦の括弧。side=right なら x から右へ開く ]、left なら [ 。"""
        d = 6 if side == "right" else -6
        self.line(x, y1, x + d, y1, color=color, check=True)
        self.line(x + d, y1, x + d, y2, color=color, check=True)
        self.line(x, y2, x + d, y2, color=color, check=True)

    def to_xml(self) -> str:
        bg = (f'<mxCell id="bg" value="" style="rounded=0;html=0;fillColor=#FFFFFF;strokeColor=none;" vertex="1" parent="1">'
              f'<mxGeometry x="0" y="0" width="{self.w}" height="{self.h:.0f}" as="geometry"/></mxCell>')
        body = "\n".join([bg] + self.cells)
        return (f'<mxfile host="financial-report-analyzer" type="device">\n<diagram id="{self.name}" name="{self.name}">\n'
                f'<mxGraphModel dx="{self.w}" dy="{self.h:.0f}" grid="0" gridSize="10" guides="1" tooltips="1" connect="0" '
                f'arrows="0" fold="0" page="0" pageScale="1" pageWidth="{self.w}" pageHeight="{self.h:.0f}" '
                f'background="#FFFFFF" math="0" shadow="0">\n<root>\n<mxCell id="0"/>\n<mxCell id="1" parent="0"/>\n'
                f'{body}\n</root>\n</mxGraphModel>\n</diagram>\n</mxfile>\n')


BREAK_AFTER = set("にのをとへでや等前外後・、／）)")
NO_BREAK_BEFORE = set("）)、。％%円ーァィゥェォッャュョヮヵヶぁぃぅぇぉっゃゅょゎ")   # 行頭に置かない文字（長音・小書き仮名を含む）


def _is_kata(ch: str) -> bool:
    return "\u30a0" <= ch <= "\u30ff"
NO_SPLIT_WORDS = {"未満", "以上", "以下", "超過"}   # 2文字の語の間で改行しない


def wrap(text: str, max_px: float, font: int) -> str:
    """表示幅で折り返す。助詞・区切り記号の直後を優先して改行位置にする。"""
    out = []
    for para in text.split("\n"):
        cur = ""
        for ch in para:
            if wide_len(cur + ch) * font > max_px and cur:
                # 直近の「改行してよい位置」まで戻る（行の半分より後ろにあるときだけ）
                # 漢字・かなからカタカナに切り替わる位置（例：「介護|クラウド」）も改行候補にする
                cut = max([i + 1 for i, c in enumerate(cur) if c in BREAK_AFTER] +
                          [i for i, c in enumerate(cur) if c in "（(" and i > 0] +
                          [i for i in range(1, len(cur)) if _is_kata(cur[i]) and not _is_kata(cur[i - 1])
                           and cur[i] not in NO_BREAK_BEFORE and cur[i - 1] not in "・"], default=0)
                if cut >= len(cur) * 0.5 and cut < len(cur):
                    out.append(cur[:cut])
                    cur = cur[cut:] + ch
                elif (ch in NO_BREAK_BEFORE or (cur[-1:] + ch) in NO_SPLIT_WORDS) and len(cur) > 1:
                    # 行頭禁則の文字は直前の文字と一緒に次の行へ送る。直前が「未満」などの語なら語ごと送る
                    k = 2 if (cur[-2:] in NO_SPLIT_WORDS and len(cur) > 2) else 1
                    while k < len(cur) - 1 and cur[-k] in NO_BREAK_BEFORE:   # 送る部分の先頭も禁則文字なら、さらに戻る
                        k += 1
                    out.append(cur[:-k])
                    cur = cur[-k:] + ch
                else:
                    out.append(cur)
                    cur = ch
            else:
                cur += ch
        out.append(cur)
    for i in range(1, len(out)):
        if len(out[i]) == 1 and len(out[i - 1]) > 2 and out[i] not in NO_BREAK_BEFORE:
            # 1文字だけの行は前の行の末尾を移して2文字以上にする。移す部分の先頭が行頭禁則の文字にならないようにする
            k = 1
            while k < len(out[i - 1]) - 1 and out[i - 1][-k] in NO_BREAK_BEFORE:
                k += 1
            out[i] = out[i - 1][-k:] + out[i]
            out[i - 1] = out[i - 1][:-k]
    return "\n".join(out)


def disp_label(label: str, kind: str | None = None) -> str:
    """図中の表示名。合計行の「合計」は図では冗長なので外す（段階利益は除く）。"""
    if kind == "subtotal":
        return label
    return re.sub(r"合計$", "", label)


def signed_label(label: str, value=None) -> str:
    """日本基準の「営業利益又は営業損失（△）」のような科目名を、金額の符号に合わせて1つの名前にする（図の表示用）。"""
    if "又は" not in label:
        return label
    m = re.match(r"^(.+?)又は(.+?)(?:（△）|\(△\))?$", label)
    if not m:
        return label
    v = D(value)
    return m.group(2) if (v is not None and v < 0) else m.group(1)


def text_h(label: str, font: int) -> float:
    return len(label.split("\n")) * font * LINE_H


# ============================================================================ 共通部品

class Ctx:
    def __init__(self, d: dict):
        self.d = d
        self.su = d["basis"]["unit"]
        pl = d.get("pl") or {}
        rev = None
        for ln in pl.get("lines", []):
            if ln.get("key") == pl.get("revenue_key", "revenue"):
                rev = D(ln.get("value"))
        ta = D(((d.get("bs") or {}).get("totals") or {}).get("assets", {}).get("value")) if d.get("bs") else None
        base = rev if rev else (ta or Decimal(0))
        self.du = choose_display_unit(base, self.su)
        self.rev = rev
        c = d["company"]
        p = d["period"]
        self.company = c.get("short_name") or c["name"]
        self.period = p["label"].replace("_", " ")
        self.span = f"{_jdate(p['start'])}〜{_jdate(p['end'])}" + ("" if p["type"] == "FY" else "（累計）")
        self.basis = f"{d['basis']['scope']}・{ {'J-GAAP': '日本基準', 'IFRS': 'IFRS', 'US-GAAP': '米国基準', 'JMIS': 'JMIS'}[d['basis']['standard']]}"
        conv = "" if self.du == self.su else f"（{self.su}単位の開示値を換算し四捨五入）"
        self.unit_note = f"単位：{self.du}{conv}"
        s1 = (d.get("sources") or [{}])[0]
        self.source = f"出典：{s1.get('title', '')}（{s1.get('published', '')}公表）"
        self._sources = {x.get("id"): x for x in d.get("sources") or []}
        if d.get("sample"):
            self.source = "【サンプルデータ：実在企業の数値ではない】" + self.source

    def use_sources(self, rows: list) -> None:
        """図で使った行の src（"S2 p.13" など）から出典表記を作り直す。"""
        ids = []
        for r in rows:
            sid = str(r.get("src") or "").split(" ")[0]
            if sid in self._sources and sid not in ids:
                ids.append(sid)
        if ids:
            prefix = "【サンプルデータ：実在企業の数値ではない】" if self.source.startswith("【サンプル") else ""
            self.source = prefix + "出典：" + "／".join(
                f"{self._sources[i].get('title', '')}（{self._sources[i].get('published', '')}公表）" for i in ids)

    def amt(self, v, places=None) -> str:
        return fmt_amount(v, self.su, self.du, places)

    def num(self, v) -> str:
        """単位なしの数値（表示単位換算後）。"""
        x = convert(v, self.su, self.du)
        if x is None:
            return "未開示"
        places = 1 if (self.du == "億円" and abs(x) < 100) else 0
        return fmt_num(x, places)


def _jdate(iso: str) -> str:
    y, m, dd = iso.split("-")
    return f"{int(y)}年{int(m)}月{int(dd)}日"


def header(cv: Canvas, ctx: Ctx, title: str, y: float = 16) -> float:
    cv.text(24, y, cv.w - 48, 26, f"{ctx.company}　{title}", font=18, bold=True)
    sub = f"{ctx.period}／{ctx.span}／{ctx.basis}／{ctx.unit_note}"
    sub = wrap(sub, cv.w - 48, 12)
    cv.text(24, y + 30, cv.w - 48, text_h(sub, 12), sub, font=12, color=SUB)
    return y + 30 + text_h(sub, 12) + 14


def footer(cv: Canvas, ctx: Ctx, y: float, extra: list[str]) -> float:
    lines = [ctx.source] + extra
    txt = wrap("\n".join(lines), cv.w - 48, 11)
    h = text_h(txt, 11)
    cv.line(24, y, cv.w - 24, y, color=GRID)
    cv.text(24, y + 8, cv.w - 48, h, txt, font=11, color=SUB)
    return y + 8 + h + 14


def stack_labels(items: list[dict], top: float, bottom: float, gap: float = 2.0):
    """外側ラベルの縦位置を重ならないよう調整する。items: {y_center, h}。"""
    items.sort(key=lambda it: it["yc"])
    for it in items:
        it["y"] = it["yc"] - it["h"] / 2
    for i in range(1, len(items)):
        prev = items[i - 1]
        if items[i]["y"] < prev["y"] + prev["h"] + gap:
            items[i]["y"] = prev["y"] + prev["h"] + gap
    # 下にはみ出したら上へ押し戻す
    if items and items[-1]["y"] + items[-1]["h"] > bottom:
        items[-1]["y"] = bottom - items[-1]["h"]
        for i in range(len(items) - 2, -1, -1):
            if items[i]["y"] + items[i]["h"] + gap > items[i + 1]["y"]:
                items[i]["y"] = items[i + 1]["y"] - gap - items[i]["h"]
    if items and items[0]["y"] < top:
        # 上端を超えた場合は上から順に詰め直す（下側に余白があれば収まる）
        items[0]["y"] = top
        for i in range(1, len(items)):
            items[i]["y"] = max(items[i]["y"], items[i - 1]["y"] + items[i - 1]["h"] + gap)
        if items[-1]["y"] + items[-1]["h"] > bottom + 200:
            raise ValueError("外側ラベルが多すぎて配置できない（data JSON 側で小さい科目をまとめること）")


def block_with_label(cv, ctx, x, y, w, h, value, scale, *, name, pct=None, fill, stroke, side, lane_x, lane_w,
                     ext, kind, dashed=False, bold=False, color=INK, stroke_width=1, highlight=False):
    """金額ブロック。収まらないラベルは外側（ext リスト）に回す。"""
    amount = ctx.amt(value)
    lab = f"{name}\n{amount}" + (f"（{pct}）" if pct else "")
    font = 12
    need_h = text_h(lab, font) + 4
    need_w = max(wide_len(s) for s in lab.split("\n")) * font + 8
    meta = {"fra_name": name, "fra_value": str(value), "fra_scale": repr(scale), "fra_kind": kind,
            "fra_display": amount}
    inside = h >= need_h and w >= need_w
    if not inside and w < need_w:
        wrapped = wrap(name, w - 10, font) + f"\n{amount}" + (f"（{pct}）" if pct else "")
        if text_h(wrapped, font) + 4 <= h and max(wide_len(x_) for x_ in wrapped.split("\n")) * font + 8 <= w:
            lab, inside = wrapped, True
    one_line = f"{name} {amount}" + (f"（{pct}）" if pct else "")
    if not inside and h >= font * LINE_H + 3 and wide_len(one_line) * font + 8 <= w:
        lab, inside = one_line, True
    cv.rect(x, y, w, h, fill, stroke, label=lab if inside else "", font=font, bold=bold or highlight, color=color,
            dashed=dashed, stroke_width=stroke_width, meta=meta)
    if not inside:
        lab = "\n".join(wrap(x_, lane_w - 18, font) for x_ in lab.split("\n"))
        ext.append({"label": lab, "yc": y + h / 2, "h": text_h(lab, font), "anchor_x": x if side == "left" else x + w,
                    "side": side, "lane_x": lane_x, "lane_w": lane_w, "color": color, "bold": bold or highlight})
    return amount


def draw_ext(cv, ext, top, bottom) -> float:
    """外側ラベルを描き、最も下のラベルの下端 y を返す。"""
    lowest = 0.0
    for side in ("left", "right"):
        items = [e for e in ext if e["side"] == side]
        if not items:
            continue
        stack_labels(items, top, bottom)
        for e in items:
            w = max(wide_len(s) for s in e["label"].split("\n")) * 12 + 2
            if side == "left":
                tx = e["lane_x"] + e["lane_w"] - w - 10
                cv.text(tx, e["y"], w, e["h"], e["label"], font=12, align="right", color=e["color"], bold=e["bold"])
                cv.line(tx + w + 2, e["y"] + e["h"] / 2, e["anchor_x"], e["yc"], color=SUB)
            else:
                tx = e["lane_x"] + 10
                cv.text(tx, e["y"], w, e["h"], e["label"], font=12, align="left", color=e["color"], bold=e["bold"])
                cv.line(e["anchor_x"], e["yc"], tx - 2, e["y"] + e["h"] / 2, color=SUB)
            lowest = max(lowest, e["y"] + e["h"])
    return lowest


def pct_of(v, base) -> str | None:
    v, base = D(v), D(base)
    if v is None or not base:
        return None
    r = v / base * 100
    if r != 0 and abs(r) < Decimal("0.05"):
        return "0.1%未満" if r > 0 else "△0.1%未満"
    return f"{round_half_up(r, 1):,.1f}%".replace("-", "△")


# ============================================================================ PL

def build_pl(d: dict) -> Canvas:
    ctx = Ctx(d)
    pl = d["pl"]
    chain = [ln for ln in pl["lines"] if ln.get("kind") in ("revenue", "income", "expense", "attribution", "subtotal")
             and not ln.get("parent")]
    split = pl.get("split_after", "operating_income")
    idx = next((i for i, ln in enumerate(chain) if ln.get("key") == split), None)
    if idx is None:
        raise ValueError(f"split_after={split} の段階利益が PL にない")
    p1, p2 = chain[: idx + 1], chain[idx:]
    profit = p1[-1]
    P = D(profit["value"])
    if P is None:
        raise ValueError(f"{profit['label']} が未開示のため PL 図を作れない")
    revs = [ln for ln in p1 if ln["kind"] in ("revenue", "income") and D(ln.get("value"))]
    exps = [ln for ln in p1 if ln["kind"] in ("expense", "attribution") and D(ln.get("value"))]
    neg_exp = [ln for ln in exps if D(ln["value"]) < 0]
    neg_rev = [ln for ln in revs if D(ln["value"]) < 0]
    if neg_exp or neg_rev:
        raise ValueError("営業段階の収益・費用に負の値がある。符号と kind を確認すること")
    base = D(ctx.rev) or sum(D(ln["value"]) for ln in revs)
    left_total = sum(D(ln["value"]) for ln in exps) + max(P, 0)
    right_total = sum(D(ln["value"]) for ln in revs) + max(-P, 0)
    tall = max(left_total, right_total)

    cv = Canvas(820, "PL")
    y = header(cv, ctx, "損益計算書（PL）の構造")
    # ---------------- パネル①
    p1_title = f"① {revs[0]['label'] if revs else '収益'}から{signed_label(profit['label'], P)}まで（面積は金額に比例、％は{_label_of(pl, pl.get('revenue_key', 'revenue'))}に対する比率）"
    p1_title = wrap(p1_title, cv.w - 48, 13)
    cv.text(24, y, cv.w - 48, text_h(p1_title, 13), p1_title, font=13, bold=True)
    y += text_h(p1_title, 13) + 10
    CW, LANE_L, LANE_R = 170, 150, 160
    x0 = (cv.w - (LANE_L + 2 * CW + LANE_R)) / 2
    lx, rx = x0 + LANE_L, x0 + LANE_L + CW
    cv.text(lx, y, CW, 18, "費用と利益", font=12, bold=True, align="center", color=SUB)
    cv.text(rx, y, CW, 18, "収益", font=12, bold=True, align="center", color=SUB)
    top = y + 22
    H1 = 380.0
    k1 = Decimal(H1) / tall
    ext = []
    displayed = []
    # 左列（費用 → 利益）
    cy = Decimal(top)
    bounds = {}
    for i, ln in enumerate(exps):
        v = D(ln["value"])
        h = v * k1
        displayed.append((ln["label"], block_with_label(cv, ctx, lx, float(cy), CW, float(h), v, float(k1),
                         name=ln["label"], pct=pct_of(v, base), fill=ORANGE[i % len(ORANGE)], stroke=ORANGE_STROKE,
                         side="left", lane_x=x0, lane_w=LANE_L, ext=ext, kind="expense")))
        bounds[ln.get("key") or ln["label"]] = (float(cy), float(cy + h))
        cy += h
    if P > 0:
        h = P * k1
        displayed.append((profit["label"], block_with_label(cv, ctx, lx, float(cy), CW, float(h), P, float(k1),
                         name=signed_label(profit["label"], P), pct=pct_of(P, base), fill=GREEN[1], stroke=GREEN_STROKE, side="left",
                         lane_x=x0, lane_w=LANE_L, ext=ext, kind="profit", bold=True)))
        cy += h
    left_bottom = cy
    # 右列（収益 → 損失）
    ry = Decimal(top)
    for i, ln in enumerate(revs):
        v = D(ln["value"])
        h = v * k1
        displayed.append((ln["label"], block_with_label(cv, ctx, rx, float(ry), CW, float(h), v, float(k1),
                         name=ln["label"], pct=pct_of(v, base), fill=TEAL[i % len(TEAL)], stroke=TEAL_STROKE,
                         side="right", lane_x=rx + CW, lane_w=LANE_R, ext=ext, kind="revenue", bold=(i == 0))))
        ry += h
    if P < 0:
        h = -P * k1
        base_lab = re.sub(r"（[^）]*）|\([^)]*\)", "", profit["label"])
        loss_label = base_lab.replace("利益", "損失") if "利益" in base_lab else base_lab + "（損失）"
        if "又は" in profit["label"]:
            loss_label = signed_label(profit["label"], P)
        displayed.append((loss_label, block_with_label(cv, ctx, rx, float(ry), CW, float(h), P, float(k1),
                         name=loss_label, pct=pct_of(P, base), fill=NEG_FILL, stroke=NEG_STROKE, side="right",
                         lane_x=rx + CW, lane_w=LANE_R, ext=ext, kind="loss", dashed=True, color=NEG_TEXT,
                         stroke_width=2, bold=True)))
        ry += h
    bottom = float(max(left_bottom, ry))
    # 中間の段階利益（売上総利益など）を右側の括弧で示す
    brackets = []
    exp_before = Decimal(0)
    for ln in p1:
        if ln["kind"] in ("expense", "attribution") and D(ln.get("value")):
            exp_before += D(ln["value"])
        if ln["kind"] == "subtotal" and ln is not profit and D(ln.get("value")) is not None:
            S = D(ln["value"])
            region = left_total - exp_before
            rev_before = sum(D(x["value"]) for x in p1[: p1.index(ln)] if x["kind"] in ("revenue", "income") and D(x.get("value")))
            if S > 0 and abs(region - S) <= 3 and rev_before == sum(D(x["value"]) for x in revs):
                y1 = top + float(exp_before * k1)
                brackets.append((ln, y1, float(left_bottom)))
    for ln, y1, y2 in brackets:
        bx = rx + CW + 8
        cv.bracket_v(bx, y1 + 1, y2 - 1, side="right")
        lab = f"{ln['label']}\n{ctx.amt(ln['value'])}\n（{pct_of(ln['value'], base)}）"
        cv.text(bx + 12, (y1 + y2) / 2 - text_h(lab, 12) / 2, LANE_R - 14, text_h(lab, 12), lab, font=12, bold=True,
                color=GREEN_STROKE)
        displayed.append((ln["label"], ctx.amt(ln["value"])))
    if brackets and any(e["side"] == "right" for e in ext):
        # 右側の外側ラベルと括弧が重なり得るので、外側ラベルは括弧より上下に逃がす
        pass
    low = draw_ext(cv, ext, top - 4, bottom + 40)
    y = max(bottom, low) + 10
    note = f"左右の列の高さはどちらも {ctx.amt(max(left_total, right_total))}。"
    if P < 0:
        note = f"費用が収益を上回った差額（{profit['label']}のマイナス）を右列の赤い破線枠で示す。" + note
    note = wrap(note, 2 * CW + 80, 11)
    cv.text(lx - 40, y, 2 * CW + 80, text_h(note, 11), note, font=11, color=SUB, align="center")
    y += text_h(note, 11) + 16

    # ---------------- パネル②（段階利益のブリッジ）
    if len(p2) > 1:
        y = bridge(cv, ctx, p2, y, k1, displayed)
    cv.h = footer(cv, ctx, y + 6, [f"％は{_label_of(pl, pl.get('revenue_key', 'revenue'))}に対する比率。△はマイナス（費用・損失・減少）を示す。",
                                   "①と②は縮尺が異なる（②は小さい金額を見やすくするため拡大）。"])
    cv.displayed = displayed
    return cv


def _label_of(pl, key):
    for ln in pl["lines"]:
        if ln.get("key") == key:
            return ln["label"]
    return key


def bridge(cv: Canvas, ctx: Ctx, steps: list[dict], y: float, k1: Decimal, displayed: list) -> float:
    """段階利益から最終利益までのウォーターフォール。"""
    # 走行値の範囲
    running = D(steps[0]["value"])
    pts = [running]
    if any(D(x.get("value")) is None and "value" not in (x.get("nil_fields") or []) for x in steps):
        pass  # 未開示の行は「未開示」と表示し、走行値は変えない
    seq = []
    from fin import fv
    for ln in steps:
        v = fv(ln, "value")
        if ln["kind"] == "subtotal":
            if v is None:
                raise ValueError(f"{ln['label']} が未開示")
            seq.append(("total", ln, v, Decimal(0), v))
            running = v
        else:
            if v is None:
                seq.append(("na", ln, None, running, running))
                continue
            eff = v if ln["kind"] in ("revenue", "income") else -v
            seq.append(("delta", ln, eff, running, running + eff))
            running += eff
        pts.append(running)
    hi, lo = max(max(pts), Decimal(0)), min(min(pts), Decimal(0))
    H2 = Decimal(200)
    k2 = H2 / (hi - lo) if hi != lo else Decimal(1)
    mag = (k2 / k1) if k1 else Decimal(1)
    scale_note = "①と同じ縮尺" if abs(mag - 1) < Decimal("0.05") else f"縮尺は①の約{round_half_up(mag, 1)}倍"
    fv0, fvn = steps[0].get("value"), steps[-1].get("value")
    t = f"② {signed_label(steps[0]['label'], fv0)}から{signed_label(steps[-1]['label'], fvn)}まで（{scale_note}、数値の単位：{ctx.du}）"
    t = wrap(t, cv.w - 48, 13)
    cv.text(24, y, cv.w - 48, text_h(t, 13), t, font=13, bold=True)
    y += text_h(t, 13) + 34   # 上に金額ラベルの余白
    n = len(seq)
    left, right = 40, cv.w - 40
    slot = (right - left) / n
    bw = min(56.0, slot * 0.62)
    base_y = y + float(hi * k2)
    cv.line(left - 10, base_y, right + 10, base_y, color=INK, width=1)
    prev_end = None
    label_bottom = base_y
    for i, (typ, ln, eff, a, b) in enumerate(seq):
        cx = left + slot * i + slot / 2
        x = cx - bw / 2
        if typ == "na":
            cv.text(x - 6, base_y - 16, bw + 12, 14, "未開示", font=11, align="center", color=SUB)
        else:
            y_top = base_y - float(max(a, b) * k2)
            h = float(abs(b - a) * k2)
            if typ == "total":
                fill, stroke = (GREEN[1], GREEN_STROKE) if b >= 0 else (NEG_FILL, NEG_STROKE)
                kind = "profit" if b >= 0 else "loss"
            else:
                fill, stroke = (TEAL[2], TEAL_STROKE) if eff >= 0 else (ORANGE[2], ORANGE_STROKE)
                kind = "plus" if eff >= 0 else "minus"
            meta = {"fra_name": ln["label"], "fra_value": str(eff if typ == "delta" else b), "fra_scale": repr(float(k2)),
                    "fra_kind": kind, "fra_display": ctx.num(eff if typ == "delta" else b)}
            cv.rect(x, y_top, bw, max(h, 0.0), fill, stroke, meta=meta, dashed=(kind == "loss"),
                    stroke_width=2 if typ == "total" else 1)
            val_txt = ctx.num(eff if typ == "delta" else b)
            if typ == "delta" and eff > 0:
                val_txt = "+" + val_txt
            if typ == "delta" and ln.get("value") is None and "value" in (ln.get("nil_fields") or []):
                val_txt = "－"   # 決算書で「－」（計上なし）
            color = NEG_TEXT if (typ == "total" and b < 0) or (typ == "delta" and eff < 0) else INK
            up = (typ == "total" and b >= 0) or (typ == "delta" and eff >= 0)
            ty = (y_top - 16) if up else (y_top + h + 2)
            cv.text(cx - slot / 2 + 1, ty, slot - 2, 14, val_txt, font=11, align="center", color=color,
                    bold=(typ == "total"), bg=True)
            displayed.append((ln["label"], val_txt))
            if prev_end is not None:
                cv.line(prev_end[0], prev_end[1], x, prev_end[1], color=GRID, dashed=True)
            prev_end = (x + bw, base_y - float(b * k2))
            label_bottom = max(label_bottom, ty + 14 if not up else base_y)
    cat_y = max(label_bottom, base_y) + 6
    max_h = 0
    for i, (typ, ln, *_r) in enumerate(seq):
        cx = left + slot * i + slot / 2
        cat = wrap(disp_label(signed_label(ln["label"], _r[0] if _r else None), ln["kind"]), slot - 6, 11)
        cv.text(cx - slot / 2 + 2, cat_y, slot - 4, text_h(cat, 11), cat, font=11, align="center",
                color=INK if ln["kind"] == "subtotal" else SUB, bold=ln["kind"] == "subtotal")
        max_h = max(max_h, text_h(cat, 11))
    return cat_y + max_h + 10


# ============================================================================ BS

# 「その他」「その他の流動資産」などの雑多な科目。xbrl_to_data が重複名に付ける「（流動）」「（固定）」「（純額）」などの
# 括弧書きが付いた「その他（流動）」も同じ扱いにする（図の集約ブロック「その他」と並んで紛らわしくなるため）
OTHER_RE = re.compile(r"その他(の(流動|固定)?(資産|負債))?(（[^）]*）)?")


def _prepare_side(lines, groups_order, total_assets, min_share, group_totals):
    """グループごとに表示する行を決め、小さい行と負の行は「その他」に集約する。"""
    out = []
    for g in groups_order:
        lns = [ln for ln in lines if (ln.get("group") or "") == g and not ln.get("parent") and D(ln.get("value")) is not None]
        gt = D((group_totals.get(g) or {}).get("value"))
        gsum = sum((D(ln["value"]) for ln in lns), Decimal(0))
        if gt is None:
            gt = gsum
        shown = [ln for ln in lns if D(ln["value"]) > 0 and (D(ln["value"]) / total_assets >= min_share or ln.get("highlight"))]
        shown = [ln for ln in shown if not OTHER_RE.fullmatch(ln["label"]) or ln.get("highlight")]
        while True:
            resid = gt - sum((D(ln["value"]) for ln in shown), Decimal(0))
            if resid >= 0 or not shown:
                break
            smallest = min((ln for ln in shown if not ln.get("highlight")), key=lambda ln: D(ln["value"]), default=None)
            if smallest is None:
                break
            shown.remove(smallest)
        items = [{"label": disp_label(ln["label"]), "value": D(ln["value"]), "highlight": bool(ln.get("highlight")),
                  "debt": bool(ln.get("interest_bearing")), "lease": bool(ln.get("lease"))} for ln in shown]
        resid = gt - sum((it["value"] for it in items), Decimal(0))
        merged = [ln["label"] for ln in lns if ln not in shown]
        # 集約した科目がなく、差が科目数以下（端数処理の差）なら「その他」を作らない
        if resid > 0 and not (not merged and resid <= len(lns)):
            name = "その他" if items else g  # グループ全体を1ブロックにまとめた場合はグループ名で表示
            items.append({"label": name, "value": resid, "highlight": False, "debt": False, "lease": False,
                          "merged": merged, "merged_group": not items})
        out.append({"group": g, "total": gt, "items": items})
    return out


def build_bs(d: dict, min_share: float = 0.025) -> Canvas:
    ctx = Ctx(d)
    bs = d["bs"]
    tot = bs["totals"]
    TA = D(tot["assets"]["value"])
    TL = D((tot.get("liabilities") or {}).get("value"))
    TN = D((tot.get("net_assets") or {}).get("value"))
    if TA is None or TL is None or TN is None:
        raise ValueError("BS 図には 資産合計・負債合計・純資産合計 が必要")
    ms = Decimal(str(min_share))
    a_groups = list(dict.fromkeys(ln.get("group") for ln in bs["assets"] if not ln.get("parent")))
    l_groups = list(dict.fromkeys(ln.get("group") for ln in bs["liabilities"] if not ln.get("parent")))
    gt = bs.get("group_totals", {})
    A = _prepare_side(bs["assets"], a_groups, TA, ms, gt)
    L = _prepare_side(bs["liabilities"], l_groups, TA, ms, gt)
    eq_lines = [ln for ln in bs["equity"] if not ln.get("parent")]
    eq_neg_component = any((D(ln.get("value")) or 0) < 0 for ln in eq_lines)
    if TN > 0 and not eq_neg_component:
        E = _prepare_side(bs["equity"], list(dict.fromkeys(ln.get("group") for ln in eq_lines)), TA, ms,
                          {**gt, **{g: {"value": TN} for g in set(ln.get("group") for ln in eq_lines)}})
    elif TN > 0:
        parts = "、".join(f"{ln['label']} {ctx.amt(ln['value'])}" for ln in eq_lines)
        eg = next((ln.get("group") for ln in eq_lines if ln.get("group")), None) or ("資本" if d["basis"]["standard"] == "IFRS" else "純資産")
        E = [{"group": eg, "total": TN, "items": [{"label": f"{eg}合計", "value": TN, "highlight": True,
                                                 "debt": False, "lease": False, "note": parts}]}]
    else:
        E = []
    left_total = TA + (-TN if TN < 0 else 0)
    right_total = TL + (TN if TN > 0 else 0)
    tall = max(left_total, right_total)

    cv = Canvas(820, "BS")
    y = header(cv, ctx, f"貸借対照表（BS）の構成　{_jdate(bs['date'])}時点")
    eqn = "資本" if d["basis"]["standard"] == "IFRS" else "純資産"
    t = f"左：集めたお金の使い道（資産）／右：お金の集め方（負債＝返済が必要、{eqn}＝返済不要）"
    t = wrap(t, cv.w - 48, 13)
    cv.text(24, y, cv.w - 48, text_h(t, 13), t, font=13, bold=True)
    y += text_h(t, 13) + 10
    CW, LANE, BR = 180, 130, 90
    x0 = (cv.w - 2 * (CW + LANE + BR)) / 2
    lx, rx = x0 + BR + LANE, x0 + BR + LANE + CW
    cv.text(lx, y, CW, 18, "資産", font=12, bold=True, align="center", color=SUB)
    cv.text(rx, y, CW, 18, f"負債・{eqn}", font=12, bold=True, align="center", color=SUB)
    top = y + 22
    H = Decimal(470)
    k = H / tall
    ext, displayed, gbr = [], [], []

    def draw_side(groups, x, side, palette_for):
        cy = Decimal(top)
        for gi, g in enumerate(groups):
            g_top = cy
            for i, it in enumerate(g["items"]):
                h = it["value"] * k
                fill, stroke, color = palette_for(g["group"], i, it)
                name = it["label"] + ("（有利子）" if it["debt"] else "")
                amount = block_with_label(cv, ctx, x, float(cy), CW, float(h), it["value"], float(k), name=name,
                                          pct=pct_of(it["value"], TA), fill=fill, stroke=stroke, side=side,
                                          lane_x=(x - LANE if side == "left" else x + CW), lane_w=LANE, ext=ext,
                                          kind="bs_item", highlight=it["highlight"],
                                          stroke_width=3 if it["highlight"] else 1, color=color)
                displayed.append((name, amount))
                cy += h
            gbr.append({"group": g["group"], "total": g["total"], "y1": float(g_top), "y2": float(cy), "side": side})
        return cy

    def pal_assets(group, i, it):
        return TEAL[i % len(TEAL)], (INK if it["highlight"] else TEAL_STROKE), INK

    def pal_right(group, i, it):
        if group in ("純資産",) or group.startswith("純資産") or group.startswith("資本"):
            return GREEN[i % len(GREEN)], (INK if it["highlight"] else GREEN_STROKE), INK
        if it["debt"]:
            return DEBT, (INK if it["highlight"] else ORANGE_STROKE), INK
        return ORANGE[i % 4], (INK if it["highlight"] else ORANGE_STROKE), INK

    ly = draw_side(A, lx, "left", pal_assets)
    if TN < 0:
        h = -TN * k
        amount = block_with_label(cv, ctx, lx, float(ly), CW, float(h), TN, float(k), name="債務超過額",
                                  pct=pct_of(TN, TA), fill=NEG_FILL, stroke=NEG_STROKE, side="left", lane_x=lx - LANE,
                                  lane_w=LANE, ext=ext, kind="deficit", dashed=True, color=NEG_TEXT, stroke_width=2)
        displayed.append(("債務超過額", amount))
        ly += h
    ry = draw_side(L + E, rx, "right", pal_right)
    bottom = float(max(ly, ry))
    # グループの括弧（ラベルは重ならないよう縦位置を調整）
    for side in ("left", "right"):
        items = []
        for g in [g for g in gbr if g["side"] == side]:
            w = BR - 8
            lab = f"{g['group']}\n{ctx.amt(g['total'])}\n（{pct_of(g['total'], TA)}）"
            items.append({"g": g, "label": lab, "yc": (g["y1"] + g["y2"]) / 2, "h": text_h(lab, 11), "w": w})
        stack_labels(items, top - 4, bottom + 40)
        for it in items:
            g = it["g"]
            if side == "left":
                bx = lx - LANE - 4
                cv.bracket_v(bx, g["y1"] + 1, g["y2"] - 1, side="left")
                cv.text(bx - 10 - it["w"], it["y"], it["w"], it["h"], it["label"], font=11, align="right", bold=True,
                        color=TEAL_STROKE)
            else:
                bx = rx + CW + LANE + 4
                cv.bracket_v(bx, g["y1"] + 1, g["y2"] - 1, side="right")
                col = GREEN_STROKE if g["group"].startswith(("純資産", "資本")) else ORANGE_STROKE
                cv.text(bx + 10, it["y"], it["w"], it["h"], it["label"], font=11, align="left", bold=True, color=col)
            displayed.append((g["group"], ctx.amt(g["total"])))
    low = draw_ext(cv, ext, top - 4, bottom + 40)
    y = max(bottom, low) + 8
    cv.text(lx, y, CW, 16, f"資産合計 {ctx.amt(TA)}", font=12, bold=True, align="center")
    lne = D((tot.get("liabilities_and_net_assets") or {}).get("value")) or (TL + TN)
    cv.text(rx - 20, y, CW + 40, 16, f"負債・{eqn}合計 {ctx.amt(lne)}", font=12, bold=True, align="center")
    displayed += [("資産合計", ctx.amt(TA)), (f"負債・{eqn}合計", ctx.amt(lne))]
    y += 22
    if TN < 0:
        eq_name = (E[0]["group"] if E else None) or next((ln.get("group") for ln in eq_lines), "純資産")
        t2 = f"（うち{eq_name}合計 {ctx.amt(TN)}＝債務超過）"
        cv.text(rx - 40, y, CW + 80, 16, t2, font=12, bold=True, align="center", color=NEG_TEXT)
        displayed.append((f"{eq_name}合計", ctx.amt(TN)))
        y += 22
    y += 8
    notes = ["％は総資産に対する比率。太枠は本文で取り上げる重要科目。（有利子）は利息の支払いが必要な負債。",
             f"総資産の{float(ms) * 100:.1f}%未満の科目と、貸倒引当金などのマイナス科目は「その他」に集約。"]
    merged = [(f"{g['group']}＝" if it.get("merged_group") else f"{g['group']}の「その他」＝") + "・".join(it["merged"])
              for g in A + L + E for it in g["items"] if it.get("merged")]
    if merged:
        notes.append("集約の内訳：" + "／".join(merged))
    if eq_neg_component and TN > 0:
        notes.append(f"{eqn}にマイナスの構成要素があるため1つにまとめて表示：" + "、".join(
            f"{ln['label']} {ctx.amt(ln['value'])}" for ln in eq_lines))
    if TN < 0:
        notes.append(f"{eqn}がマイナス（債務超過）のため、その金額を資産側の赤い破線枠で示し、左右の高さをそろえている。")
    cv.h = footer(cv, ctx, y, notes)
    cv.displayed = displayed
    return cv


# ============================================================================ CF

def build_cf(d: dict) -> Canvas:
    ctx = Ctx(d)
    cf = d["cf"]
    if cf.get("disclosed") is False:
        raise ValueError("CF 未開示")
    B, E = D(cf["begin"]["value"]), D(cf["end"]["value"])
    flows = [ln for ln in cf["lines"] if not ln.get("parent") and ln.get("kind") != "info"]
    if B is None or E is None or any(D(ln.get("value")) is None for ln in flows):
        raise ValueError("期首・期末残高または CF の主要項目が未開示")
    seq = [("bal", cf["begin"].get("label", "期首残高"), B, Decimal(0), B)]
    run = B
    for ln in flows:
        v = D(ln["value"])
        seq.append(("flow", ln["label"], v, run, run + v))
        run += v
    seq.append(("bal", cf["end"].get("label", "期末残高"), E, Decimal(0), E))
    diff = E - run
    hi = max(max(max(a, b) for *_x, a, b in seq), Decimal(0))
    lo = min(min(min(a, b) for *_x, a, b in seq), Decimal(0))

    cv = Canvas(820, "CF")
    y = header(cv, ctx, "キャッシュ・フロー（CF）の流れ")
    t = wrap(f"{cf.get('cash_definition', '現金及び現金同等物')}の期首残高が、営業・投資・財務の各活動でどう増減して期末残高になったか（上向き＝増加、下向き＝減少）",
             cv.w - 48, 13)
    cv.text(24, y, cv.w - 48, text_h(t, 13), t, font=13, bold=True)
    y += text_h(t, 13) + 12
    # FCF 表示帯
    fcf_band = 40
    y += fcf_band
    H = Decimal(320)
    k = H / (hi - lo)
    n = len(seq)
    left, right = 40, cv.w - 40
    slot = (right - left) / n
    bw = min(78.0, slot * 0.6)
    base_y = y + 18 + float(hi * k)
    cv.line(left - 10, base_y, right + 10, base_y, color=INK)
    displayed, prev, xs = [], None, {}
    lab_bottom = base_y
    for i, (typ, label, v, a, b) in enumerate(seq):
        cx = left + slot * i + slot / 2
        x = cx - bw / 2
        y_top = base_y - float(max(a, b) * k)
        h = float(abs(b - a) * k)
        if typ == "bal":
            fill, stroke, kind = CASH, CASH_STROKE, "balance"
        elif v >= 0:
            fill, stroke, kind = POS_FLOW, POS_STROKE, "inflow"
        else:
            fill, stroke, kind = NEG_FILL, NEG_STROKE, "outflow"
        meta = {"fra_name": label, "fra_value": str(v), "fra_scale": repr(float(k)), "fra_kind": kind,
                "fra_display": ctx.amt(v)}
        cv.rect(x, y_top, bw, max(h, 0.0), fill, stroke, meta=meta, stroke_width=2 if typ == "bal" else 1,
                dashed=(kind == "outflow"))
        amt = ctx.amt(v)
        if typ == "flow" and v > 0:
            amt = "+" + amt
        up = typ == "bal" or v >= 0
        ty = (y_top - 17) if up else (y_top + h + 3)
        cv.text(cx - slot / 2 + 1, ty, slot - 2, 15, wrap(amt, slot - 4, 12), font=12, align="center", bold=(typ == "bal"),
                color=NEG_TEXT if (typ == "flow" and v < 0) else INK, bg=True)
        displayed.append((label, amt))
        if not up:
            lab_bottom = max(lab_bottom, ty + text_h(wrap(amt, slot - 4, 12), 12))
        if prev is not None:
            cv.line(prev[0], prev[1], x, prev[1], color=GRID, dashed=True)
        prev = (x + bw, base_y - float(b * k))
        xs[label] = (x, y_top, h)
    # カテゴリ名
    cat_y = max(lab_bottom, base_y) + 8
    mh = 0
    for i, (typ, label, *_r) in enumerate(seq):
        cx = left + slot * i + slot / 2
        lab = wrap(_short_cf(label), slot - 6, 11)
        cv.text(cx - slot / 2 + 2, cat_y, slot - 4, text_h(lab, 11), lab, font=11, align="center",
                bold=(typ == "bal"), color=INK if typ == "bal" else SUB)
        mh = max(mh, text_h(lab, 11))
    # FCF
    keys = {ln.get("key"): ln for ln in flows}
    extra = []
    if "cfo" in keys and "cfi" in keys:
        fcf = D(keys["cfo"]["value"]) + D(keys["cfi"]["value"])
        i1 = 1 + flows.index(keys["cfo"])
        i2 = 1 + flows.index(keys["cfi"])
        x1 = left + slot * i1 + slot / 2 - bw / 2
        x2 = left + slot * i2 + slot / 2 + bw / 2
        by = y - fcf_band + 6
        cv.line(x1, by + 22, x1, by + 14, color=SUB)
        cv.line(x1, by + 14, x2, by + 14, color=SUB)
        cv.line(x2, by + 14, x2, by + 22, color=SUB)
        txt = f"フリーキャッシュフロー（営業CF＋投資CF）＝ {'+' if fcf > 0 else ''}{ctx.amt(fcf)}"
        w = wide_len(txt) * 12 + 4
        cv.text(max(24, (x1 + x2) / 2 - w / 2), by - 4, w, 15, txt, font=12, bold=True,
                color=NEG_TEXT if fcf < 0 else GREEN_STROKE)
        displayed.append(("フリーキャッシュフロー", ctx.amt(fcf)))
    chk = f"照合：期首 {ctx.amt(B)} ＋ 各増減の合計 {ctx.amt(run - B)} ＝ {ctx.amt(run)}（開示された期末残高 {ctx.amt(E)}、差 {fmt_num(diff)} {ctx.su}）"
    extra.append(chk)
    if cf.get("cash_reconciliation_note"):
        extra.append(cf["cash_reconciliation_note"])
    extra.append("緑＝現金の増加、赤い破線＝現金の減少、青＝残高。棒の高さは金額に比例。")
    cv.h = footer(cv, ctx, cat_y + mh + 12, extra)
    cv.displayed = displayed
    cv.cf_diff = diff
    return cv


def _short_cf(label: str) -> str:
    rep = [("営業活動によるキャッシュ・フロー", "営業CF"), ("投資活動によるキャッシュ・フロー", "投資CF"),
           ("財務活動によるキャッシュ・フロー", "財務CF"), ("現金及び現金同等物に係る換算差額", "為替換算差額"),
           ("現金及び現金同等物の期首残高", "期首残高"), ("現金及び現金同等物の期末残高", "期末残高"),
           ("現金及び現金同等物の", "")]
    for a, b in rep:
        if label == a:
            return b
    if "為替" in label and "現金" in label:   # 為替変動による現金及び現金同等物への影響額 など
        return "為替変動の影響"
    if "売却目的" in label:                    # 売却目的で保有する資産への振替に伴う現金及び現金同等物の増減額
        return "売却目的資産への振替"
    label = re.sub(r"（△は[^）]*）", "", label)   # 符号の注記は図では不要
    for a, b in rep[-1:]:
        label = label.replace(a, b)
    return label


# ============================================================================ セグメント構成（補助図）

SEG_COLORS = ["#4e9fd6", "#f0a04b", "#59b38a", "#d77fa1", "#9b8ad1", "#c9b458", "#7fc3d9", "#e08a6e"]
SEG_OTHER = "#b9bec2"   # 「その他」は灰色


def _seg_colors(items) -> list[str]:
    """セグメントごとの色。売上高と利益の円で同じ色を使う。"""
    cols, k = [], 0
    for it in items:
        if str(it["name"]).startswith("その他"):
            cols.append(SEG_OTHER)
        else:
            cols.append(SEG_COLORS[k % len(SEG_COLORS)])
            k += 1
    return cols


def draw_pie(cv, ctx, *, panel_x, panel_w, top, title, sub, values, names, colors, pie_id, displayed, dia=170) -> float:
    """円グラフ1つ（扇形、外側の名前と構成比、引き出し線）。values は正の値。描いた領域の下端 y を返す。"""
    cv.text(panel_x, top, panel_w, 18, title, font=13, bold=True, align="center")
    cv.text(panel_x, top + 20, panel_w, 16, sub, font=11, color=SUB, align="center")
    total = sum(values)
    R = dia / 2
    cx = panel_x + panel_w / 2
    pie_top = top + 20 + 16 + 34          # 上側の外側ラベル用の余白
    cy = pie_top + R
    lane = (panel_w - dia) / 2 - 16
    labels = {"left": [], "right": []}
    acc = Decimal(0)
    for v, nm, col in zip(values, names, colors):
        st = float(acc / total)
        acc += v
        en = float(acc / total)
        cv.pie_slice(cx - R, cy - R, dia, st, en, col,
                     meta={"fra_name": nm, "fra_value": str(v), "fra_total": str(total), "fra_kind": "pie_slice",
                           "fra_pie": pie_id, "fra_display": ctx.amt(v)})
        m = (st + en) / 2 * 2 * math.pi
        ex, ey = cx + R * 0.9 * math.sin(m), cy - R * 0.9 * math.cos(m)
        ox, oy = cx + (R + 8) * math.sin(m), cy - (R + 8) * math.cos(m)
        side = "right" if math.sin(m) >= 0 else "left"
        lab = wrap(nm, lane, 11) + "\n" + pct_of(v, total)
        labels[side].append({"label": lab, "yc": cy - (R + 16) * math.cos(m), "h": text_h(lab, 11),
                             "ax": ex, "ay": ey, "ox": ox, "oy": oy})
        displayed.append((f"{nm} {title}", f"{ctx.amt(v)}（{pct_of(v, total)}）"))
    lowest = cy + R
    for side, items in labels.items():
        if not items:
            continue
        stack_labels(items, pie_top - 30, cy + R + 30, gap=3)
        for e in items:
            w = max(wide_len(x_) for x_ in e["label"].split("\n")) * 11 + 2
            my = e["y"] + e["h"] / 2
            if side == "right":
                tx = cx + R + 16
                cv.text(tx, e["y"], w, e["h"], e["label"], font=11, align="left")
                cv.line(e["ax"], e["ay"], e["ox"], e["oy"], color=SUB)
                cv.line(e["ox"], e["oy"], tx - 3, my, color=SUB)
            else:
                tx = cx - R - 16 - w
                cv.text(tx, e["y"], w, e["h"], e["label"], font=11, align="right")
                cv.line(e["ax"], e["ay"], e["ox"], e["oy"], color=SUB)
                cv.line(e["ox"], e["oy"], tx + w + 3, my, color=SUB)
            lowest = max(lowest, e["y"] + e["h"])
    return lowest


def build_seg(d: dict) -> Canvas:
    """セグメントごとの売上高と利益の構成比を円グラフ2つで示し、金額・構成比・利益率の表を付ける。"""
    ctx = Ctx(d)
    segs = [s_ for s_ in (d.get("segments") or []) if D(s_.get("revenue")) is not None]
    use_profit = len(segs) >= 2 and all(D(s_.get("profit")) is not None for s_ in segs)
    items, title = segs, "セグメント別の売上高と利益の構成"
    if len(segs) < 2:
        bu = d.get("revenue_breakdown_unit") or ctx.su   # 内訳が財務諸表と別の単位で開示されている場合は換算する
        items = [{"name": x["label"], "revenue": convert(x["value"], bu, ctx.su), "src": x.get("src")}
                 for x in (d.get("revenue_breakdown") or [])]
        title, use_profit = "売上高の内訳", False
    ctx.use_sources(items)
    if len(items) < 2:
        raise ValueError("セグメント（または revenue_breakdown）が2つ未満")
    R = [D(x["revenue"]) for x in items]
    if any(r < 0 for r in R):
        raise ValueError("売上高が負のセグメントがある")
    TR = sum(R)
    P = [D(x.get("profit")) for x in items] if use_profit else []
    colors = _seg_colors(items)
    pos = [i for i, pv in enumerate(P) if pv > 0]
    neg = [i for i, pv in enumerate(P) if pv <= 0]
    PT = sum((P[i] for i in pos), Decimal(0))
    profit_pie = use_profit and len(pos) >= 2
    cv = Canvas(820, "SEG")
    y = header(cv, ctx, title)
    t = "円の中心角は金額に比例。％は各円の合計に対する構成比。色はセグメントを表す（左右の円で同じ色は同じセグメント）"
    if not profit_pie:
        t = "円の中心角は金額に比例。％は合計に対する構成比"
    t = wrap(t, cv.w - 48, 13)
    cv.text(24, y, cv.w - 48, text_h(t, 13), t, font=13, bold=True)
    y += text_h(t, 13) + 16
    displayed: list = []
    ri = [i for i, r in enumerate(R) if r > 0]
    rev_kw = dict(top=y, title="売上高の構成", sub=f"合計 {ctx.amt(TR)}", values=[R[i] for i in ri],
                  names=[items[i]["name"] for i in ri], colors=[colors[i] for i in ri], pie_id="revenue",
                  displayed=displayed)
    if profit_pie:
        low1 = draw_pie(cv, ctx, panel_x=24, panel_w=386, **rev_kw)
        low2 = draw_pie(cv, ctx, panel_x=410, panel_w=386, top=y, title="セグメント利益の構成",
                        sub=f"黒字セグメントの合計 {ctx.amt(PT)}", values=[P[i] for i in pos],
                        names=[items[i]["name"] for i in pos], colors=[colors[i] for i in pos], pie_id="profit",
                        displayed=displayed)
        y = max(low1, low2) + 20
    else:
        y = draw_pie(cv, ctx, panel_x=24, panel_w=cv.w - 48, **rev_kw) + 20

    # ---- 金額・構成比・利益率の表
    cols = [("", 24, 12, "left"), ("セグメント" if len(segs) >= 2 else "区分", 42, 200, "left"),
            ("売上高", 242, 120, "right"), ("構成比", 362, 70, "right")]
    if use_profit:
        cols += [("セグメント利益", 432, 130, "right"), ("構成比", 562, 80, "right"), ("利益率", 642, 154, "right")]
    RH = 20
    for name, x, w, al in cols[1:]:
        cv.text(x, y, w, 15, name, font=11, bold=True, color=SUB, align=al)
    y += RH
    cv.line(24, y - 3, 796, y - 3, color=GRID)
    for i, it in enumerate(items):
        nm = wrap(it["name"], 196, 11)
        h = max(RH, text_h(nm, 11) + 5)
        cv.rect(24, y + (h - 12) / 2 - 2, 12, 12, colors[i], "#FFFFFF", track_text=False)
        cells = [nm, ctx.amt(R[i]), pct_of(R[i], TR) or "―"]
        cell_colors = [INK, INK, INK]
        if use_profit:
            pv = P[i]
            rt = D(it.get("revenue_total")) or R[i]   # 利益率の分母は内部売上を含むセグメント売上計（あれば）
            share = pct_of(pv, PT) if (i in pos and profit_pie) else ("円に含めず" if pv <= 0 and profit_pie else "―")
            cells += [ctx.amt(pv), share, pct_of(pv, rt) or "―"]
            c = NEG_TEXT if pv < 0 else INK
            cell_colors += [c, INK, c]
            displayed.append((f"{it['name']} 利益", ctx.amt(pv)))
        for (cname, x, w, al), txt, col in zip(cols[1:], cells, cell_colors):
            cv.text(x, y + (h - text_h(txt, 11)) / 2 - 2, w, text_h(txt, 11), txt, font=11, align=al, color=col)
        y += h
    cv.line(24, y - 1, 796, y - 1, color=GRID)
    cells = ["合計", ctx.amt(TR), "100.0%"]
    if use_profit:
        tp = sum(P)
        cells += [ctx.amt(tp), "―", pct_of(tp, TR) or "―"]
    for (cname, x, w, al), txt in zip(cols[1:], cells):
        cv.text(x, y + 2, w, 15, txt, font=11, bold=True, align=al, color=NEG_TEXT if txt.startswith("△") else INK)
    y += RH + 8

    notes = [f"売上高の合計 {ctx.amt(TR)}（表示した区分の合計。セグメント間の内部売上の扱いは出典に従う）。" if len(segs) >= 2
             else f"売上高の合計 {ctx.amt(TR)}（表示した区分の合計）。"]
    if use_profit:
        s0 = (d.get("segments") or [{}])[0]
        basis = s0.get("profit_basis", "セグメント利益（調整額控除前）")
        # セグメント利益と照合する連結の段階利益（日本基準では経常利益と照合する会社がある）
        rk = s0.get("profit_reconciles_to", "operating_income")
        rl = next((ln for ln in d["pl"]["lines"] if ln.get("key") == rk), None)
        op = D(rl.get("value")) if rl else None
        rname = signed_label(rl["label"], op) if rl else "営業利益"
        adj = D(s0.get("adjustment"))
        tp = sum(P)
        if op is not None and tp == op:
            notes.append(f"利益は{basis}。セグメント利益の合計 {ctx.amt(tp)} は連結の{rname}と一致する（調整額なし）。")
        elif op is not None and adj is not None and abs(tp + adj - op) <= len(P) + 1:
            notes.append(f"利益は{basis}。セグメント利益の合計 {ctx.amt(tp)} に調整額 {ctx.amt(adj)}（全社費用など）を加えると、"
                         f"連結の{rname}（{ctx.amt(op)}）になる。")
        else:
            notes.append(f"利益は{basis}。合計 {ctx.amt(tp)} は全社費用などの調整額を含まないため、連結の{rname}"
                         f"（{ctx.amt(op) if op is not None else '―'}）とは一致しない。")
        if profit_pie and neg:
            lst = "、".join(f"{items[i]['name']} {ctx.amt(P[i])}" for i in neg)
            notes.append(f"赤字（または利益ゼロ）のセグメント（{lst}）は構成比を表せないため利益の円に含めない。"
                         f"利益の円の構成比は黒字セグメントの利益の合計（{ctx.amt(PT)}）に対する比率。")
        elif profit_pie:
            notes.append("利益の構成比はセグメント利益の合計に対する比率。")
        else:
            notes.append("黒字のセグメントが1つ以下のため、利益の円グラフは作らない（金額と利益率は表を参照）。")
        if any(x.get("revenue_total") for x in items):
            notes.append("売上高は外部顧客に対する売上。利益率はセグメント間の内部売上を含むセグメント売上計に対する比率（会社開示と同じ定義）。")
        else:
            notes.append("利益率は表示した売上高に対する比率。")
    else:
        if len(segs) < 2 and d.get("revenue_breakdown_unit") and d["revenue_breakdown_unit"] != ctx.su:
            notes.append(f"内訳は{d['revenue_breakdown_unit']}単位の開示値。端数処理のため合計は損益計算書の売上高と一致しない場合がある。")
        notes.append("単一セグメントのため、決算説明資料などの売上内訳を表示。" if len(segs) < 2 else
                     "セグメント利益が開示されていないため、売上高の円グラフだけを表示。")
    cv.h = footer(cv, ctx, y, notes)
    cv.displayed = displayed
    return cv


# ============================================================================ 書き出しと検証

def drawio_cli() -> str:
    c = load_config().get("drawio_cli")
    for cand in [c, shutil.which("drawio"), "/Applications/draw.io.app/Contents/MacOS/draw.io"]:
        if cand and Path(cand).exists():
            return cand
    raise FileNotFoundError("draw.io Desktop が見つからない。https://www.drawio.com からインストールするか config の drawio_cli を設定")


def export(drawio_path: Path, fmt: str = "svg", scale: float | None = None) -> Path:
    out = drawio_path.with_suffix("." + fmt)
    cmd = [drawio_cli(), "-x", "-f", fmt, "-b", "0", "--theme", "light", "-o", str(out)]
    if fmt in ("svg", "png"):
        cmd.insert(4, "-e")
    if scale:
        cmd += ["-s", str(scale)]
    cmd.append(str(drawio_path))
    # 環境変数 FRA_DRAWIO_ARGS で draw.io（Electron）に追加の引数を渡せる（例：Linux の CI では "--no-sandbox"）
    cmd += shlex.split(os.environ.get("FRA_DRAWIO_ARGS", ""))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if not out.exists():
        raise RuntimeError(f"draw.io の書き出しに失敗: {r.stdout} {r.stderr}")
    return out


def verify(drawio_path: Path, manifest: dict | None = None) -> dict:
    errs, warns = [], []
    tree = ET.parse(drawio_path)
    root = tree.getroot()
    n_blocks = 0
    pies: dict[str, list] = {}
    for obj in root.iter("object"):
        if "fra_value" not in obj.attrib:
            continue
        n_blocks += 1
        geo = obj.find("mxCell/mxGeometry")
        if obj.get("fra_kind") == "pie_slice":
            # 扇形：中心角の割合 ＝ 金額 ÷ 円の合計（誤差 1e-5 以内）。円は正円であること
            style = obj.find("mxCell").get("style", "")
            st = float(re.search(r"startAngle=([\d.]+)", style).group(1))
            en = float(re.search(r"endAngle=([\d.]+)", style).group(1))
            frac = float(Decimal(obj.get("fra_value")) / Decimal(obj.get("fra_total")))
            if float(obj.get("fra_value")) <= 0:
                errs.append(f"{obj.get('fra_name')}: 円グラフに0以下の値がある")
            if abs((en - st) - frac) > 3e-6:   # 6桁表記の丸め（各端 5e-7）と終端の 0.999999 を許容
                errs.append(f"{obj.get('fra_name')}: 扇形の割合 {en - st:.6f} が金額から計算した {frac:.6f} と一致しない")
            if abs(float(geo.get("width")) - float(geo.get("height"))) > 0.01:
                errs.append(f"{obj.get('fra_name')}: 円グラフが正円でない")
            pies.setdefault(obj.get("fra_pie"), []).append((st, en, obj.get("fra_name")))
            continue
        dim = "width" if obj.get("fra_axis") == "x" else "height"
        h = float(geo.get(dim))
        exp = abs(float(obj.get("fra_value"))) * float(obj.get("fra_scale"))
        if abs(h - exp) > 0.6:
            errs.append(f"{obj.get('fra_name')}: {'幅' if dim == 'width' else '高さ'} {h:.2f}px が金額から計算した {exp:.2f}px と一致しない")
    for pid, sl in pies.items():
        sl.sort()
        if abs(sl[0][0]) > 1e-6 or sl[-1][1] < 0.999998:
            errs.append(f"円グラフ {pid}: 扇形が 0〜1 周を覆っていない（{sl[0][0]:.6f}〜{sl[-1][1]:.6f}）")
        for a, b in zip(sl, sl[1:]):
            if abs(a[1] - b[0]) > 1e-6:
                errs.append(f"円グラフ {pid}: 「{a[2]}」と「{b[2]}」の間に隙間または重なりがある")
    svg_path = drawio_path.with_suffix(".svg")
    svg_texts = []
    if svg_path.exists():
        s = svg_path.read_text(encoding="utf-8")
        svg_texts = [re.sub(r"<[^>]+>", "", t).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                     for t in re.findall(r"<text[^>]*>(.*?)</text>", s, re.S)]
        if "light-dark(" in s:
            errs.append("SVG にダークモード用の配色が含まれている（--theme light で書き出すこと）")
        m = re.search(r'<svg[^>]*width="([\d.]+)px"[^>]*height="([\d.]+)px"', s)
        if m and manifest and abs(float(m.group(1)) - manifest["width"]) > 2:
            warns.append(f"SVG 幅 {m.group(1)} が想定 {manifest['width']} と異なる")
    else:
        errs.append("SVG が存在しない")
    if manifest:
        svg_set = set(svg_texts)
        for t in manifest["texts"]:
            for line in t["lines"]:
                if line and line not in svg_set:
                    errs.append(f"SVG にテキスト「{line}」が出力されていない（文字切れ・省略の可能性）")
        for t in manifest["texts"]:
            for (sx1, sy1, sx2, sy2) in manifest.get("segs", []):
                if t["x"] + 1 < sx2 and sx1 < t["x"] + t["w"] - 1 and t["y"] + 1 < sy2 + 0.5 and sy1 - 0.5 < t["y"] + t["h"] - 1:
                    errs.append(f"テキスト「{t['lines'][0]}」が括弧・軸の線と重なっている")
                    break
        # テキスト同士の重なり（推定外接矩形）
        T = manifest["texts"]
        for i in range(len(T)):
            a = T[i]
            if a["x"] < -1 or a["x"] + a["w"] > manifest["width"] + 1 or a["y"] < -1 or a["y"] + a["h"] > manifest["height"] + 1:
                errs.append(f"テキスト「{a['lines'][0]}」がキャンバス外にはみ出している")
            if a.get("container"):
                cx, cy, cw, ch = a["container"]
                if a["w"] > cw + 0.5 or a["h"] > ch + 0.5:
                    errs.append(f"テキスト「{' '.join(a['lines'])}」が図形（{cw:.0f}×{ch:.0f}px）に収まっていない")
            for j in range(i + 1, len(T)):
                b = T[j]
                ox = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
                oy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
                if ox > 1.0 and oy > 1.0:
                    errs.append(f"テキストが重なっている：「{a['lines'][0]}」と「{b['lines'][0]}」（{ox:.0f}×{oy:.0f}px）")
    return {"file": str(drawio_path), "ok": not errs, "errors": errs, "warnings": warns, "n_value_blocks": n_blocks,
            "n_svg_texts": len(svg_texts)}


def build(data_path: str, out_dir: str, prefix: str | None, png: bool) -> dict:
    d = json.loads(Path(data_path).read_text(encoding="utf-8"))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    prefix = prefix or d["period"]["label"]
    res = {"ok": True, "figures": [], "skipped": []}
    builders = [("PL", build_pl), ("BS", build_bs), ("CF", build_cf), ("SEG", build_seg)]
    for kind, fn in builders:
        if kind == "BS" and not d.get("bs"):
            res["skipped"].append({"figure": kind, "reason": "BS データなし"})
            continue
        if kind == "CF" and (not d.get("cf") or d["cf"].get("disclosed") is False):
            res["skipped"].append({"figure": kind, "reason": (d.get("cf") or {}).get("reason_if_not", "CF 未開示")})
            continue
        if kind == "SEG" and len(d.get("segments") or []) < 2 and len(d.get("revenue_breakdown") or []) < 2:
            res["skipped"].append({"figure": kind, "reason": "セグメント・売上内訳が2つ未満（補助図は作らない）"})
            continue
        try:
            cv = fn(d)
        except ValueError as e:
            res["ok"] = False
            res["skipped"].append({"figure": kind, "reason": str(e)})
            continue
        p = out / f"{prefix}_{kind}.drawio"
        p.write_text(cv.to_xml(), encoding="utf-8")
        svg = export(p, "svg")
        if png:
            export(p, "png", scale=1.5)
        manifest = {"width": cv.w, "height": cv.h, "texts": cv.texts, "segs": cv.segs}
        v = verify(p, manifest)
        if not v["ok"]:
            res["ok"] = False
        res["figures"].append({"figure": kind, "drawio": str(p), "svg": str(svg),
                               "png": str(p.with_suffix(".png")) if png else None, "size": [cv.w, round(cv.h)],
                               "verify": v, "values": [{"label": a, "display": b} for a, b in cv.displayed],
                               "cf_end_diff": str(getattr(cv, "cf_diff", "")) or None})
    import hashlib
    res["version"] = VERSION
    res["data"] = str(Path(data_path))
    res["data_sha256"] = hashlib.sha256(Path(data_path).read_bytes()).hexdigest()
    (out / f"{prefix}_figures.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("build")
    b.add_argument("data")
    b.add_argument("--out-dir", required=True)
    b.add_argument("--prefix")
    b.add_argument("--png", action="store_true", help="目視確認用の PNG も書き出す")
    v = sp.add_parser("verify")
    v.add_argument("drawio", nargs="+")
    a = ap.parse_args()
    if a.cmd == "build":
        r = build(a.data, a.out_dir, a.prefix, a.png)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        sys.exit(0 if r["ok"] else 1)
    else:
        rs = [verify(Path(p)) for p in a.drawio]
        print(json.dumps(rs, ensure_ascii=False, indent=2))
        sys.exit(0 if all(r["ok"] for r in rs) else 1)


if __name__ == "__main__":
    main()
