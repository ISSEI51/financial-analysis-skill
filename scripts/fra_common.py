"""financial-report-analyzer 共通モジュール（標準ライブラリのみ使用）。

- 設定ファイルの読み書き
- HTTP 取得（キャッシュ付き）
- 金額の単位変換と日本語表記
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

VERSION = "1.1.0"
SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("FRA_CONFIG", Path.home() / ".config" / "financial-report-analyzer" / "config.json"))

DEFAULT_CONFIG = {
    "vault_path": None,               # Obsidian Vault の絶対パス
    "root_folder": "決算分析",          # Vault 直下に作るフォルダ名
    "cache_dir": str(Path.home() / ".cache" / "financial-report-analyzer"),
    "drawio_cli": None,               # None の場合は自動検出
    "edinet_api_key_env": "EDINET_API_KEY",
    "user_agent": "Mozilla/5.0 (compatible; financial-report-analyzer/1.0)",
    "embed_width": 760,               # Obsidian 埋め込み時の表示幅(px)
    "debt_includes_lease": False,     # 有利子負債にリース負債を含めるか（既定は含めず別掲）
    "display_unit": "auto",           # auto / 百万円 / 億円
}

UNIT_TO_YEN = {
    "円": Decimal(1),
    "千円": Decimal(1_000),
    "百万円": Decimal(1_000_000),
    "億円": Decimal(100_000_000),
    "十億円": Decimal(1_000_000_000),
    "兆円": Decimal(1_000_000_000_000),
}


# ---------------------------------------------------------------- 設定

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError as e:
            die(f"設定ファイルの JSON が不正です: {CONFIG_PATH}: {e}")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cache_dir(*parts: str) -> Path:
    d = Path(os.path.expanduser(load_config()["cache_dir"])).joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- 出力

def die(msg: str, code: int = 2) -> None:
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False), file=sys.stderr)
    sys.exit(code)


def emit(obj) -> None:
    """machine-readable な結果を stdout に JSON で出す。"""
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- HTTP

def http_get(url: str, *, headers: dict | None = None, timeout: int = 30, retries: int = 2,
             binary: bool = True):
    cfg = load_config()
    h = {"User-Agent": cfg["user_agent"]}
    if headers:
        h.update(headers)
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                return data if binary else data.decode(r.headers.get_content_charset() or "utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (404, 401, 403):
                raise
            last = e
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"取得失敗: {url}: {last}")


def download(url: str, dest_dir: Path, filename: str | None = None) -> dict:
    """URL を dest_dir に保存し、sha256 等のメタデータを返す。

    取得記録（.<名前>.meta.json）に URL と取得日時を残す。同じ URL を既に保存済みなら、
    保存名が違っても再取得せずに既存ファイルを返す（cached=true、retrieved_at は最初の取得日時）。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    for meta_p in dest_dir.glob(".*.meta.json"):
        try:
            m = json.loads(meta_p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if m.get("url") == url and Path(m.get("path", "")).exists():
            data = Path(m["path"]).read_bytes()
            if hashlib.sha256(data).hexdigest() == m.get("sha256"):
                return {**m, "cached": True}
    name = filename or Path(urllib.parse.urlparse(url).path).name or "download.bin"
    dest = dest_dir / name
    old_meta = dest_dir / f".{name}.meta.json"
    if dest.exists() and old_meta.exists():
        try:
            other = json.loads(old_meta.read_text(encoding="utf-8")).get("url")
        except json.JSONDecodeError:
            other = None
        if other and other != url:
            # 同名だが別の URL から取得したファイル（IR 配信サービスの 00.pdf など）。上書き・流用せず別名で保存する
            p = Path(name)
            name = f"{p.stem}_{hashlib.sha256(url.encode()).hexdigest()[:8]}{p.suffix}"
            dest = dest_dir / name
    cached = dest.exists()
    if not cached:
        dest.write_bytes(http_get(url))
    data = dest.read_bytes()
    meta = {"url": url, "path": str(dest), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "retrieved_at": now_iso() if not cached else None}
    if cached:
        meta["note"] = "同名のファイルが既にあり、取得元 URL の記録がない。内容を確認すること"
    (dest_dir / f".{name}.meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {**meta, "cached": cached}


# ---------------------------------------------------------------- 数値

def D(x) -> Decimal | None:
    if x is None:
        return None
    if isinstance(x, Decimal):
        return x
    if isinstance(x, (int,)):
        return Decimal(x)
    if isinstance(x, float):
        return Decimal(repr(x))
    s = str(x).strip().replace(",", "").replace("△", "-").replace("▲", "-").replace("−", "-")
    if s in ("", "-", "―", "—", "NULL", "null", "None", "未開示"):
        return None
    return Decimal(s)


def convert(value, from_unit: str, to_unit: str) -> Decimal | None:
    v = D(value)
    if v is None:
        return None
    return v * UNIT_TO_YEN[from_unit] / UNIT_TO_YEN[to_unit]


def round_half_up(v: Decimal, places: int = 0) -> Decimal:
    q = Decimal(1).scaleb(-places)
    return v.quantize(q, rounding=ROUND_HALF_UP)


def fmt_num(v, places: int = 0, neg: str = "△", plus: bool = False) -> str:
    """3桁区切り。負数は △ 表記（決算短信の慣行）。"""
    v = D(v)
    if v is None:
        return "未開示"
    r = round_half_up(v, places)
    s = f"{abs(r):,.{places}f}"
    if r < 0:
        return f"{neg}{s}"
    if plus and r > 0:
        return f"+{s}"
    return s


def choose_display_unit(max_abs_value, source_unit: str) -> str:
    """表示単位の自動選択。売上規模が 100 億円以上なら 億円、未満なら 百万円。"""
    cfg_unit = load_config().get("display_unit", "auto")
    if cfg_unit != "auto":
        return cfg_unit
    yen = convert(max_abs_value, source_unit, "円") or Decimal(0)
    return "億円" if abs(yen) >= Decimal(10_000_000_000) else "百万円"


def fmt_amount(value, source_unit: str, display_unit: str, places: int | None = None) -> str:
    """金額を表示単位に換算して「1,234億円」の形にする。1兆円以上は「1兆2,345億円」。"""
    v = convert(value, source_unit, display_unit)
    if v is None:
        return "未開示"
    if places is None:
        places = 0
        if display_unit == "億円" and abs(v) < 100:
            places = 1
    if display_unit == "億円" and abs(v) >= 10_000:
        r = round_half_up(v, 0)
        sign = "△" if r < 0 else ""
        cho, oku = divmod(int(abs(r)), 10_000)
        return f"{sign}{cho:,}兆{oku:,}億円" if oku else f"{sign}{cho:,}兆円"
    return fmt_num(v, places) + display_unit


def pct(numer, denom, places: int = 1) -> Decimal | None:
    n, d = D(numer), D(denom)
    if n is None or d is None or d == 0:
        return None
    return round_half_up(n / d * 100, places)


def fmt_pct(v, places: int = 1, plus: bool = False) -> str:
    v = D(v)
    if v is None:
        return "算出不可"
    return fmt_num(v, places, plus=plus) + "%"


def wide_len(s: str) -> float:
    """表示幅の概算（全角=1.0、半角=0.55）。図のレイアウト計算に使う。"""
    w = 0.0
    for ch in s:
        o = ord(ch)
        if o < 0x2E80 or 0xFF61 <= o <= 0xFF9F:
            w += 0.62 if ch.isupper() else 0.56
        else:
            w += 1.0
    return w
