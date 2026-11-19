#!/usr/bin/env python3
"""Obsidian Vault との連携（Vault 検出、フォルダ作成、ノートの安全な更新、企業一覧・相互リンクの再生成）。

ノートの構造（自動生成部分はマーカーで囲む。マーカーの外側は一切書き換えない）:

  ---
  frontmatter（スクリプト管理のキーだけ更新し、利用者が追加したキーは残す）
  ---
  <!-- fra:nav:start --> … <!-- fra:nav:end -->     ← 相互リンク（毎回再生成）
  <!-- fra:auto:start --> … <!-- fra:auto:end -->   ← 分析本文（fra_auto_hash で人手編集を検出）
  ## メモ                                            ← 利用者の自由記述（変更しない）

使い方:
  python3 -I obsidian.py detect
  python3 -I obsidian.py setup [--vault PATH] [--root 決算分析]
  python3 -I obsidian.py paths --name 任天堂株式会社 --code 7974 [--period 2027年3月期_第2四半期] [--vault PATH]
  python3 -I obsidian.py init-company --name 任天堂株式会社 --code 7974 [--industry その他製品] [--vault PATH]
  python3 -I obsidian.py upsert --note <path.md> --body <body.md> --frontmatter <fm.json> [--force]
  python3 -I obsidian.py relink --code 7974 [--vault PATH]       # 企業概要の決算一覧・各決算ノートの前後リンク・企業一覧を再生成
  python3 -I obsidian.py refresh-tables --note <path.md> --data <data.json>   # 表だけをデータから作り直す
  python3 -I obsidian.py embeds --code 7974 --period 2027年3月期_第2四半期 [--vault PATH]

終了コード: 0=成功, 2=引数・環境エラー, 3=Vault を利用者に確認する必要あり, 4=人手編集を検出したため更新を中止
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import urllib.parse
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fra_common import CONFIG_PATH, cache_dir, die, emit, load_config, save_config  # noqa: E402

AUTO_S, AUTO_E = "<!-- fra:auto:start -->", "<!-- fra:auto:end -->"
NAV_S, NAV_E = "<!-- fra:nav:start -->", "<!-- fra:nav:end -->"
LIST_S, LIST_E = "<!-- fra:list:start -->", "<!-- fra:list:end -->"
MEMO_HEADER = "## メモ（自由記述）\n\n<!-- この見出しより下は自動更新で変更されません。気づいたことを自由に書いてください。 -->\n"
PERIOD_ORDER = {"第1四半期": 1, "第2四半期": 2, "第3四半期": 3, "本決算": 4}
MANAGED_KEYS = ["会社名", "証券コード", "業種", "決算期", "対象期間", "資料公表日", "分析日", "会計基準", "連結・単体",
                "企業概要", "tags", "fra_type", "fra_period", "fra_data", "fra_sample", "fra_auto_hash", "fra_updated"]


# ============================================================================ 排他制御

class VaultLock:
    """同じ Vault への書き込み（upsert / relink / 企業一覧の更新）を直列化する。ロックファイルは Vault の外に置く。"""

    def __init__(self, vault: Path):
        h = hashlib.sha256(str(Path(vault).resolve()).encode()).hexdigest()[:12]
        self.path = cache_dir("locks") / f"{h}.lock"

    def __enter__(self):
        import fcntl
        self.f = open(self.path, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


# ============================================================================ Vault 検出

def obsidian_registry() -> Path | None:
    home = Path.home()
    cands = {
        "Darwin": [home / "Library/Application Support/obsidian/obsidian.json"],
        "Linux": [home / ".config/obsidian/obsidian.json",
                  home / ".var/app/md.obsidian.Obsidian/config/obsidian/obsidian.json"],
        "Windows": [Path(os.environ.get("APPDATA", "")) / "obsidian/obsidian.json"],
    }.get(platform.system(), [])
    return next((p for p in cands if p.exists()), None)


def detect() -> dict:
    reg = obsidian_registry()
    vaults = []
    if reg:
        try:
            j = json.loads(reg.read_text(encoding="utf-8"))
            for vid, v in (j.get("vaults") or {}).items():
                p = Path(v.get("path", ""))
                if p.exists() and (p / ".obsidian").is_dir():
                    vaults.append({"path": str(p), "open": bool(v.get("open")), "last_opened": v.get("ts")})
        except json.JSONDecodeError:
            pass
    source = "obsidian.json"
    if not vaults:   # 登録情報がない場合はホーム配下を浅く探す
        source = "filesystem"
        for base in [Path.home() / "Documents", Path.home()]:
            for p in base.glob("*/.obsidian"):
                vaults.append({"path": str(p.parent), "open": False, "last_opened": None})
    vaults.sort(key=lambda v: (not v["open"], -(v["last_opened"] or 0)))
    return {"registry": str(reg) if reg else None, "source": source, "candidates": vaults}


def resolve_vault(arg: str | None) -> Path:
    if arg:
        p = Path(arg).expanduser()
        if not p.is_dir():
            die(f"Vault のパスが存在しない: {p}")
        return p
    cfg = load_config()
    if cfg.get("vault_path") and Path(cfg["vault_path"]).is_dir():
        return Path(cfg["vault_path"])
    die("Vault が未設定。`obsidian.py setup` を実行すること", code=3)


def root_dir(vault: Path) -> Path:
    return vault / load_config().get("root_folder", "決算分析")


# ============================================================================ フォルダ・パス

CORP_RE = re.compile(r"^(株式会社|（株）|\(株\))\s*|\s*(株式会社|（株）|\(株\))$")


def folder_name(official: str, code: str) -> str:
    """公式な商号から法人格表記（株式会社）を除いた名称 + (証券コード)。"""
    name = CORP_RE.sub("", official.strip())
    name = re.sub(r'[\\/:*?"<>|#^\[\]]', "＿", name)   # ファイル名・リンクで使えない文字
    return f"{name}({code})"


def find_company_dir(vault: Path, code: str) -> Path | None:
    r = root_dir(vault)
    if not r.exists():
        return None
    for p in r.iterdir():
        if p.is_dir() and p.name.endswith(f"({code})"):
            return p
    return None


def company_dir(vault: Path, name: str | None, code: str) -> Path:
    existing = find_company_dir(vault, code)   # 社名表記が変わっても同じコードのフォルダを再利用し重複を防ぐ
    if existing:
        return existing
    if not name:
        die(f"証券コード {code} のフォルダがなく、--name も指定されていない")
    return root_dir(vault) / folder_name(name, code)


def vault_rel(vault: Path, p: Path) -> str:
    return p.relative_to(vault).as_posix()


def period_sort_key(stem: str):
    m = re.match(r"(\d{4})年(\d{1,2})月期_(.+)", stem)
    if not m:
        return (9999, 99, 9)
    return (int(m.group(1)), int(m.group(2)), PERIOD_ORDER.get(m.group(3), 9))


def period_notes(cdir: Path) -> list[Path]:
    return sorted([p for p in cdir.glob("*.md") if re.match(r"\d{4}年\d{1,2}月期_", p.stem)],
                  key=lambda p: period_sort_key(p.stem))


# ============================================================================ frontmatter

def split_note(text: str):
    """(frontmatter の行リスト, 本文) を返す。frontmatter が無ければ ([], text)。"""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[4:end].split("\n"), text[end + 5:]
        if text.rstrip().endswith("\n---"):
            return text[4:text.rstrip().rfind("\n---")].split("\n"), ""
    return [], text


def fm_blocks(lines: list[str]) -> list[tuple[str | None, list[str]]]:
    """トップレベルのキーごとに行をまとめる（値の解釈はしない）。"""
    blocks: list[tuple[str | None, list[str]]] = []
    for ln in lines:
        m = re.match(r"^([^\s#:][^:]*):(\s|$)", ln)
        if m and not ln.startswith((" ", "\t", "-")):
            blocks.append((m.group(1).strip(), [ln]))
        elif blocks:
            blocks[-1][1].append(ln)
        else:
            blocks.append((None, [ln]))
    return blocks


def fm_parse_simple(lines: list[str]) -> dict:
    """スクリプトが書いた形式（key: value / ブロックリスト）を読む。"""
    out = {}
    for key, blk in fm_blocks(lines):
        if key is None:
            continue
        first = blk[0].split(":", 1)[1].strip()
        if first == "" and len(blk) > 1:
            out[key] = [re.sub(r"^\s*-\s*", "", x).strip().strip('"') for x in blk[1:] if x.strip().startswith("-")]
        else:
            out[key] = first.strip('"')
    return out


def yaml_scalar(v) -> str:
    if v is None:
        return '""'
    s = str(v)
    if s == "" or re.search(r"[:#\[\]{}&*!|>'\"%@`,]|^\s|\s$|^[-?]", s) or re.fullmatch(r"[\d.]+", s) or s.lower() in ("true", "false", "null", "yes", "no"):
        return json.dumps(s, ensure_ascii=False)
    return s


def fm_render(existing_lines: list[str], managed: dict) -> str:
    """利用者が追加したキーは残し、管理キーだけ上書きする。"""
    blocks = fm_blocks(existing_lines)
    out, seen = [], set()
    for key, blk in blocks:
        if key in managed:
            seen.add(key)
            out.extend(_render_kv(key, managed[key]))
        elif key is not None or any(x.strip() for x in blk):
            out.extend(blk)
    for key in MANAGED_KEYS + [k for k in managed if k not in MANAGED_KEYS]:
        if key in managed and key not in seen:
            out.extend(_render_kv(key, managed[key]))
    return "---\n" + "\n".join(out) + "\n---\n"


def _render_kv(key, v):
    if isinstance(v, list):
        return [f"{key}:"] + [f"  - {yaml_scalar(x)}" for x in v]
    return [f"{key}: {yaml_scalar(v)}"]


def body_hash(s: str) -> str:
    return hashlib.sha256(s.strip().encode("utf-8")).hexdigest()[:16]


def extract_block(text: str, s: str, e: str) -> str | None:
    i, j = text.find(s), text.find(e)
    if i == -1 or j == -1 or j < i:
        return None
    return text[i + len(s):j]


def replace_block(text: str, s: str, e: str, new_inner: str, *, insert_at_top=False) -> str:
    i, j = text.find(s), text.find(e)
    blk = f"{s}\n{new_inner.strip()}\n{e}"
    if i == -1 or j == -1:
        return (blk + "\n\n" + text.lstrip("\n")) if insert_at_top else (text.rstrip("\n") + "\n\n" + blk + "\n")
    return text[:i] + blk + text[j + len(e):]


# ============================================================================ ノート更新

def upsert(note: Path, body: str, managed_fm: dict, force: bool = False) -> dict:
    note.parent.mkdir(parents=True, exist_ok=True)
    new_hash = body_hash(body)
    managed_fm = dict(managed_fm)
    managed_fm["fra_auto_hash"] = new_hash
    managed_fm["fra_updated"] = datetime.now().isoformat(timespec="seconds")
    if not note.exists():
        text = fm_render([], managed_fm) + f"{NAV_S}\n{NAV_E}\n\n{AUTO_S}\n{body.strip()}\n{AUTO_E}\n\n{MEMO_HEADER}"
        note.write_text(text, encoding="utf-8")
        return {"ok": True, "action": "created", "note": str(note)}
    old = note.read_text(encoding="utf-8")
    fm_lines, rest = split_note(old)
    old_fm = fm_parse_simple(fm_lines)
    cur = extract_block(rest, AUTO_S, AUTO_E)
    backup = None
    if cur is not None:
        recorded = old_fm.get("fra_auto_hash")
        edited = recorded and body_hash(cur) != recorded
        if edited and not force:
            prop = cache_dir("proposed") / f"{note.stem}.{datetime.now():%Y%m%d%H%M%S}.md"
            prop.write_text(body, encoding="utf-8")
            return {"ok": False, "action": "aborted_manual_edit", "note": str(note),
                    "message": "自動生成部分（fra:auto）が人手で編集されている。上書きすると編集が失われるため中止した。"
                               "人手の追記を「メモ」欄へ移すか、利用者に確認してから --force で再実行すること。",
                    "proposed_body": str(prop)}
        if edited or force:
            backup = cache_dir("backups") / f"{note.stem}.{datetime.now():%Y%m%d%H%M%S}.md"
            shutil.copy2(note, backup)
        if body_hash(cur) == new_hash:
            # 本文に変化なし。frontmatter だけ更新
            managed_fm["fra_updated"] = old_fm.get("fra_updated", managed_fm["fra_updated"])
        rest = replace_block(rest, AUTO_S, AUTO_E, body)
    else:
        # マーカーが無い既存ノート（人が作ったノート）には本文を追記し、既存内容は残す
        backup = cache_dir("backups") / f"{note.stem}.{datetime.now():%Y%m%d%H%M%S}.md"
        shutil.copy2(note, backup)
        rest = rest.rstrip("\n") + f"\n\n{AUTO_S}\n{body.strip()}\n{AUTO_E}\n"
    if NAV_S not in rest:
        rest = f"{NAV_S}\n{NAV_E}\n\n" + rest.lstrip("\n")
    if "## メモ" not in rest:
        rest = rest.rstrip("\n") + "\n\n" + MEMO_HEADER
    new = fm_render(fm_lines, managed_fm) + rest
    changed = new != old
    if changed:
        note.write_text(new, encoding="utf-8")
    return {"ok": True, "action": "updated" if changed else "unchanged", "note": str(note),
            "backup": str(backup) if backup else None}


# ============================================================================ 相互リンク・一覧

def wl(vault: Path, p: Path, alias: str | None = None) -> str:
    rel = vault_rel(vault, p.with_suffix("")) if p.suffix == ".md" else vault_rel(vault, p)
    return f"[[{rel}|{alias}]]" if alias else f"[[{rel}]]"


def relink(vault: Path, code: str) -> dict:
    cdir = find_company_dir(vault, code)
    if not cdir:
        die(f"証券コード {code} の企業フォルダがない")
    notes = period_notes(cdir)
    overview = cdir / "企業概要.md"
    changed = []
    for i, n in enumerate(notes):
        prev_ = wl(vault, notes[i - 1], notes[i - 1].stem.replace("_", " ")) if i > 0 else "（なし）"
        next_ = wl(vault, notes[i + 1], notes[i + 1].stem.replace("_", " ")) if i + 1 < len(notes) else "（なし）"
        nav = (f"> [!info] ナビゲーション\n> {wl(vault, overview, '企業概要')} ／ {wl(vault, root_dir(vault) / '企業一覧.md', '企業一覧')}\n"
               f"> 前の決算：{prev_} ／ 次の決算：{next_}")
        t = n.read_text(encoding="utf-8")
        fm, rest = split_note(t)
        rest2 = replace_block(rest, NAV_S, NAV_E, nav, insert_at_top=True)
        if rest2 != rest:
            n.write_text(("---\n" + "\n".join(fm) + "\n---\n" if fm else "") + rest2, encoding="utf-8")
            changed.append(str(n))
    # 企業概要の決算一覧（スクリプト管理ブロック）
    rows = ["| 決算期 | 対象期間 | 資料公表日 | 分析日 | ノート |", "|---|---|---|---|---|"]
    for n in reversed(notes):
        f = fm_parse_simple(split_note(n.read_text(encoding="utf-8"))[0])
        rows.append(f"| {f.get('決算期', n.stem)} | {f.get('対象期間', '')} | {f.get('資料公表日', '')} | {f.get('分析日', '')} | "
                    f"{wl(vault, n, n.stem.replace('_', ' '))} |")
    lst = "### 決算ファイル一覧（自動更新）\n\n" + "\n".join(rows)
    hist = cdir / "data"
    if hist.is_dir() and any(hist.glob("*.json")):
        from fin import history as fin_history
        h = fin_history(str(hist))
        if h["periods"]:
            lst += "\n\n### 主要数値の推移（自動更新・筆者計算）\n\n"
            ni_head = "純利益（親会社帰属）" if all(p["scope"] != "単体" for p in h["periods"]) else \
                ("純利益" if all(p["scope"] == "単体" for p in h["periods"]) else "純利益（連結は親会社帰属）")
            lst += f"| 決算期 | 会計基準 | 売上高 | 営業利益 | {ni_head} | 営業利益率 | 自己資本比率 |\n|---|---|--:|--:|--:|--:|--:|\n"
            from fra_common import D, fmt_num, fmt_pct
            for p in h["periods"]:
                lst += (f"| {p['label'].replace('_', ' ')} | {p['standard']}・{p['scope']} | {fmt_num(D(p['revenue']) if p['revenue'] != 'None' else None)} | "
                        f"{fmt_num(D(p['operating_income']) if p['operating_income'] != 'None' else None)} | "
                        f"{fmt_num(D(p['net_income_parent']) if p['net_income_parent'] != 'None' else None)} | "
                        f"{fmt_pct(D(p['operating_margin'])) if p['operating_margin'] else '―'} | {fmt_pct(D(p['equity_ratio'])) if p['equity_ratio'] else '―'} |\n")
            units = {p["unit"] for p in h["periods"]}
            lst += f"\n単位：{'・'.join(sorted(units))}。四半期は累計値（単独四半期ではない）。期の種類が異なる行同士は直接比較しないこと。"
            if h["comparability_notes"]:
                lst += "\n\n> [!warning] 比較可能性\n" + "\n".join(f"> - {x}" for x in h["comparability_notes"])
    if overview.exists():
        t = overview.read_text(encoding="utf-8")
        fm, rest = split_note(t)
        rest2 = replace_block(rest, LIST_S, LIST_E, lst)
        if rest2 != rest:
            overview.write_text(("---\n" + "\n".join(fm) + "\n---\n" if fm else "") + rest2, encoding="utf-8")
            changed.append(str(overview))
    idx = update_index(vault)
    return {"ok": True, "company_dir": str(cdir), "period_notes": [str(n) for n in notes], "changed": changed,
            "index": idx}


def update_index(vault: Path) -> str:
    r = root_dir(vault)
    r.mkdir(parents=True, exist_ok=True)
    idx = r / "企業一覧.md"
    rows = ["| 企業 | 証券コード | 業種 | 最新の決算ノート | 分析日 | 決算ノート数 |", "|---|---|---|---|---|--:|"]
    for c in sorted([p for p in r.iterdir() if p.is_dir() and re.search(r"\(\w{4}\)$", p.name)], key=lambda p: p.name):
        ov = c / "企業概要.md"
        f = fm_parse_simple(split_note(ov.read_text(encoding="utf-8"))[0]) if ov.exists() else {}
        notes = period_notes(c)
        latest = notes[-1] if notes else None
        lf = fm_parse_simple(split_note(latest.read_text(encoding="utf-8"))[0]) if latest else {}
        sample = " （サンプル）" if f.get("fra_sample") == "true" or lf.get("fra_sample") == "true" else ""
        rows.append(f"| {wl(vault, ov, c.name.rsplit('(', 1)[0]) if ov.exists() else c.name}{sample} | {c.name.rsplit('(', 1)[1].rstrip(')')} | "
                    f"{f.get('業種', '')} | {wl(vault, latest, latest.stem.replace('_', ' ')) if latest else '―'} | "
                    f"{lf.get('分析日', '')} | {len(notes)} |")
    body = "\n".join(rows)
    if not idx.exists():
        idx.write_text("---\ntags:\n  - 決算分析\nfra_type: index\n---\n# 企業一覧\n\n"
                       "financial-report-analyzer で分析した企業の一覧。表はスクリプトが自動更新する。\n\n"
                       f"{LIST_S}\n{body}\n{LIST_E}\n\n{MEMO_HEADER}", encoding="utf-8")
    else:
        t = idx.read_text(encoding="utf-8")
        t2 = replace_block(t, LIST_S, LIST_E, body)
        if t2 != t:
            idx.write_text(t2, encoding="utf-8")
    return str(idx)


def init_company(vault: Path, name: str, code: str, industry: str | None, sample: bool) -> dict:
    cdir = company_dir(vault, name, code)
    created = not cdir.exists()
    for sub in ("", "assets", "data"):
        (cdir / sub).mkdir(parents=True, exist_ok=True)
    ov = cdir / "企業概要.md"
    if not ov.exists():
        fm = {"会社名": name, "証券コード": code, "業種": industry or "",
              "tags": ["決算分析", f"決算分析/{code}", "企業概要"] + (["サンプル"] if sample else []),
              "fra_type": "company_overview", "fra_sample": "true" if sample else "false"}
        title = f"# {name}（{code}）企業概要\n\n"
        warn = "> [!warning] サンプルデータ\n> このフォルダはテスト用のサンプルです。実企業の分析結果ではありません。\n\n" if sample else ""
        ov.write_text(fm_render([], fm) + title + warn +
                      f"{AUTO_S}\n（企業概要は分析時に自動生成されます）\n{AUTO_E}\n\n{LIST_S}\n{LIST_E}\n\n{MEMO_HEADER}",
                      encoding="utf-8")
    update_index(vault)
    return {"ok": True, "company_dir": str(cdir), "created": created, "overview": str(ov),
            "assets_dir": str(cdir / "assets"), "data_dir": str(cdir / "data")}


def embeds(vault: Path, code: str, period: str, kinds: list[str] | None = None) -> str:
    cdir = find_company_dir(vault, code)
    if not cdir:
        die(f"証券コード {code} の企業フォルダがない")
    width = load_config().get("embed_width", 760)
    out = []
    names = {"PL": "損益計算書（PL）", "BS": "貸借対照表（BS）", "CF": "キャッシュ・フロー（CF）", "SEG": "セグメント・売上構成"}
    for kind in (kinds or ["PL", "BS", "CF", "SEG"]):
        svg = cdir / "assets" / f"{period}_{kind}.svg"
        dio = cdir / "assets" / f"{period}_{kind}.drawio"
        if not svg.exists():
            continue
        url = "file://" + urllib.parse.quote(str(dio))
        out.append(f"<!-- fra:fig:{kind} -->\n![[{vault_rel(vault, svg)}|{width}]]\n"
                   f"*図：{names[kind]}。編集用ファイル：[{dio.name}]({url})（`{vault_rel(vault, dio)}`。draw.io で開く。SVG にも図のデータを埋め込み済み）*")
    return "\n\n".join(out)


# ============================================================================ 表の再生成

def refresh_tables(note: Path, data: Path) -> dict:
    """ノートの fra:table ブロックだけをデータ JSON から作り直す（本文の文章は変えない）。

    スクリプトの更新やデータの修正のあとに使う。自動生成部分が人手で編集されていれば中止する。
    """
    from fin import TABLES, load as load_data
    d = load_data(data)
    text = note.read_text(encoding="utf-8")
    fm_lines, rest = split_note(text)
    fm = fm_parse_simple(fm_lines)
    cur = extract_block(rest, AUTO_S, AUTO_E)
    if cur is None:
        return {"ok": False, "action": "no_auto_block", "note": str(note)}
    if fm.get("fra_auto_hash") and body_hash(cur) != fm["fra_auto_hash"]:
        return {"ok": False, "action": "aborted_manual_edit", "note": str(note),
                "message": "自動生成部分が人手で編集されているため表を更新しない"}
    changed = []

    def rep(m):
        k = m.group(1)
        if k not in TABLES or (k == "bs" and not d.get("bs")):
            return m.group(0)
        new = TABLES[k](d).strip()
        if new != m.group(0).strip():
            changed.append(k)
        return new
    body = re.sub(r"<!-- fra:table:(\w+) -->.*?<!-- /fra:table:\1 -->", rep, cur, flags=re.S)
    if not changed:
        return {"ok": True, "action": "unchanged", "note": str(note)}
    backup = cache_dir("backups") / f"{note.stem}.{datetime.now():%Y%m%d%H%M%S}.md"
    shutil.copy2(note, backup)
    managed = {k: fm[k] for k in MANAGED_KEYS if k in fm}
    r = upsert(note, body, managed, force=False)
    return {**r, "tables_changed": changed, "backup": str(backup)}


# ============================================================================ CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("detect")
    s = sp.add_parser("setup")
    s.add_argument("--vault")
    s.add_argument("--root")
    for c in ("paths", "init-company", "relink", "embeds"):
        x = sp.add_parser(c)
        if c == "embeds":
            x.add_argument("--kind", help="PL,BS,CF,SEG のうち出力する図（カンマ区切り。省略時はすべて）")
        x.add_argument("--vault")
        x.add_argument("--code", required=True)
        x.add_argument("--name")
        x.add_argument("--period")
        x.add_argument("--industry")
        x.add_argument("--sample", action="store_true")
    rt = sp.add_parser("refresh-tables")
    rt.add_argument("--note", required=True)
    rt.add_argument("--data", required=True)
    u = sp.add_parser("upsert")
    u.add_argument("--note", required=True)
    u.add_argument("--body", required=True)
    u.add_argument("--frontmatter", required=True)
    u.add_argument("--force", action="store_true")
    a = ap.parse_args()

    if a.cmd == "detect":
        cfg = load_config()
        emit({"configured_vault": cfg.get("vault_path"), **detect()})
    elif a.cmd == "setup":
        cfg = load_config()
        if a.root:
            cfg["root_folder"] = a.root
        if a.vault:
            v = Path(a.vault).expanduser()
            if not (v / ".obsidian").is_dir():
                die(f"{v} は Obsidian Vault ではない（.obsidian フォルダがない）")
            cfg["vault_path"] = str(v)
        else:
            det = detect()
            c = det["candidates"]
            if len(c) == 1 and det["source"] == "obsidian.json":
                cfg["vault_path"] = c[0]["path"]
            else:
                emit({"ok": False, "need_user_choice": True, "candidates": c,
                      "message": "Vault を一意に特定できない。利用者に選んでもらい --vault で指定すること"})
                sys.exit(3)
        save_config(cfg)
        emit({"ok": True, "config": cfg, "config_path": str(CONFIG_PATH)})
    elif a.cmd == "paths":
        v = resolve_vault(a.vault)
        cdir = company_dir(v, a.name, a.code)
        res = {"vault": str(v), "root": str(root_dir(v)), "company_dir": str(cdir), "exists": cdir.exists(),
               "overview": str(cdir / "企業概要.md"), "assets_dir": str(cdir / "assets"), "data_dir": str(cdir / "data"),
               "existing_periods": [p.stem for p in period_notes(cdir)] if cdir.exists() else []}
        if a.period:
            note = cdir / f"{a.period}.md"
            res.update({"note": str(note), "note_exists": note.exists(), "data": str(cdir / "data" / f"{a.period}.json"),
                        "data_exists": (cdir / "data" / f"{a.period}.json").exists()})
        emit(res)
    elif a.cmd == "init-company":
        v = resolve_vault(a.vault)
        if not a.name:
            die("--name（公式な商号）が必要")
        with VaultLock(v):
            emit(init_company(v, a.name, a.code, a.industry, a.sample))
    elif a.cmd == "upsert":
        fm = json.loads(Path(a.frontmatter).read_text(encoding="utf-8"))
        body = Path(a.body).read_text(encoding="utf-8")
        note = Path(a.note)
        vault_guess = next((p for p in [note, *note.parents] if (p / ".obsidian").is_dir()), note.parent)
        with VaultLock(vault_guess):
            r = upsert(note, body, fm, a.force)
        emit(r)
        sys.exit(0 if r["ok"] else 4)
    elif a.cmd == "refresh-tables":
        note = Path(a.note)
        vault_guess = next((p for p in [note, *note.parents] if (p / ".obsidian").is_dir()), note.parent)
        with VaultLock(vault_guess):
            r = refresh_tables(note, Path(a.data))
        emit(r)
        sys.exit(0 if r["ok"] else 4)
    elif a.cmd == "relink":
        v = resolve_vault(a.vault)
        with VaultLock(v):
            emit(relink(v, a.code))
    elif a.cmd == "embeds":
        if not a.period:
            die("--period が必要")
        print(embeds(resolve_vault(a.vault), a.code, a.period, a.kind.split(",") if a.kind else None))


if __name__ == "__main__":
    main()
