set shell := ["bash", "-c"]
set dotenv-load := true
set dotenv-filename := ".env"

# financial-report-analyzer（Claude Code スキル）の操作入口。
# スキルなので up/down/reset/seed/logs/port/wt は持たない。

# レシピ一覧
default:
    @just --list --unsorted

# テスト一式（一時 Vault を使う。利用者の Vault と設定は変更しない）
test:
    python3 -I tests/run_tests.py

# TDnet への接続テストも含める
test-live:
    python3 -I tests/run_tests.py --live

# テストを1つだけ（名前の一部で指定。例: just test-one diagrams）
test-one target:
    python3 -I tests/run_tests.py --only {{target}}

# 構文チェック
lint:
    #!/usr/bin/env bash
    set -euo pipefail
    fail=0
    for f in scripts/*.py tests/*.py; do
      python3 -m py_compile "$f" || { echo "{\"file\": \"$f\", \"status\": \"fail\"}"; fail=1; }
    done
    if command -v node >/dev/null; then
      for f in scripts/*.cjs; do
        node --check "$f" || { echo "{\"file\": \"$f\", \"status\": \"fail\"}"; fail=1; }
      done
    fi
    echo "{\"lint\": \"$([ $fail -eq 0 ] && echo pass || echo fail)\"}"
    exit $fail

# 型チェック（型注釈の構文確認のみ。mypy があれば実行）
typecheck:
    #!/usr/bin/env bash
    if command -v mypy >/dev/null; then mypy --ignore-missing-imports scripts; else echo '{"typecheck": "skipped", "reason": "mypy not installed"}'; fi

# CI 相当（lint と test を並列実行してまとめて返す）
ci:
    #!/usr/bin/env bash
    set -uo pipefail
    tmp=$(mktemp -d)
    (just lint > "$tmp/lint.log" 2>&1; echo $? > "$tmp/lint.rc") &
    (just test > "$tmp/test.log" 2>&1; echo $? > "$tmp/test.rc") &
    wait
    for j in lint test; do echo "{\"job\": \"$j\", \"exit\": $(cat $tmp/$j.rc)}"; done
    tail -n 1 "$tmp/test.log"
    rc=$(( $(cat $tmp/lint.rc) + $(cat $tmp/test.rc) ))
    rm -rf "$tmp"
    exit $rc

# 現在の設定（Vault など）を表示
config:
    python3 -I scripts/obsidian.py detect
