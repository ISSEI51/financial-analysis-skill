# financial-analysis-skill

上場企業の決算資料を一次資料から取得・解析し、Obsidian に決算分析ノートを作る [Claude Code](https://claude.com/claude-code) 用のスキルです（スキル名：`financial-report-analyzer`）。

- 決算短信・決算説明資料・有価証券報告書などの一次資料から数値を取得し、各数値に出典（資料名・URL・頁）を付けます。
- 財務三表（PL・BS・CF）とセグメント構成を、金額に比例した draw.io 図（`.drawio` と SVG）にします。
- ビジネスモデルと7つの観点（ビジネスモデル・収益性・成長性・安全性・効率性・将来性・リスク）から、【事実】【解釈】【仮説】を分けて分析します。
- 投資判断を勧める文書ではなく、決算の内容を理解するための学習用資料を作ります。

## できること

| 機能 | 内容 |
|---|---|
| 起動 | `/financial-report-analyzer 7974`、または「村田製作所の最新決算を分析して」「この決算短信PDFを読んで」などの自然文 |
| 資料の取得 | TDnet（直近約1か月）、JPX の適時開示（それより古い決算短信・XBRL）、企業の IR サイト、EDINET API（API キーがある場合） |
| 数値の抽出 | 決算短信の XBRL（日本基準・IFRS、連結・単体、千円・百万円）から下書きのデータ JSON を作成。XBRL がない場合は PDF から転記 |
| 検証 | 段階利益・BS の合計・CF の期首→期末を照合し、丸め誤差と本当の不一致を区別。未開示の値は 0 で埋めず未開示として扱う |
| 計算 | 指標・差額・比率・億円換算をすべてスクリプト（Decimal）で計算し、定義を表示 |
| 図 | PL（売上高から利益までの面積図と利益の内訳）、BS（資産と負債・純資産の構成）、CF（ウォーターフォール）、セグメント（売上高と利益の構成比の円グラフ）。書き出し後に、大きさが金額に比例しているか、文字の欠落や重なりがないかを自動で検証 |
| Obsidian | `決算分析/` フォルダに企業ごとのノートを作成。同じ決算期は重複させずに更新し、利用者が書いたメモは消さない。企業一覧・企業概要・決算期ノートを相互にリンク |
| 最終チェック | 本文中の数値を、同じ文に出てくる科目名のデータと照合。表・図・出典 URL の記載漏れも確認 |

## 必要なもの

| ソフトウェア | 用途 | 備考 |
|---|---|---|
| Claude Code | スキルの実行 | |
| Python 3 | すべてのスクリプト | 標準ライブラリのみ使用。3.12 と 3.14 で動作確認 |
| poppler（`pdftotext`・`pdfinfo`・`pdftoppm`） | PDF のテキスト抽出と頁の画像化 | macOS：`brew install poppler` |
| draw.io Desktop | 図の SVG・PNG 書き出し | macOS：`brew install --cask drawio` |
| Obsidian | ノートの保存先（Vault） | |
| just（任意） | テストの実行 | `brew install just` |
| Node.js と Playwright の Chromium（任意） | `scripts/browser_fetch.cjs` | HTTP クライアントからの取得を受け付けない IR サイトで、画面付きブラウザから PDF を保存する場合だけ使う |
| EDINET API キー（任意） | 有価証券報告書の取得 | 環境変数 `EDINET_API_KEY` |

## インストール

スキル名に合わせて `financial-report-analyzer` というフォルダに配置します。

```bash
git clone https://github.com/ISSEI51/financial-analysis-skill.git ~/.claude/skills/financial-report-analyzer
```

初回の実行時に、Obsidian の設定（`obsidian.json`）から Vault を自動で検出します。Vault が複数ある場合や見つからない場合は、Claude Code が保存先を質問します。

## 使い方

Claude Code で次のように依頼します。

```text
/financial-report-analyzer 6981
村田製作所の最新決算を分析して
7974 の 2027年3月期 第2四半期をまとめて
~/Downloads/tanshin.pdf を読んで決算ノートを作って
前回決算から何が変わった？
```

複数の企業を続けて依頼することもできます（例：「スマレジ（4431）, ブロードリーフ（3673） も作成して」）。

## 設定

設定ファイルは `~/.config/financial-report-analyzer/config.json` です（環境変数 `FRA_CONFIG` で変更可）。

| キー | 既定値 | 内容 |
|---|---|---|
| `vault_path` | 自動検出 | Obsidian Vault の絶対パス |
| `root_folder` | `決算分析` | Vault 直下に作るフォルダ名 |
| `cache_dir` | `~/.cache/financial-report-analyzer` | 取得した資料の保存先 |
| `drawio_cli` | 自動検出 | draw.io の実行ファイルのパス |
| `embed_width` | `760` | ノートに埋め込む図の表示幅（px） |
| `display_unit` | `auto` | 本文の金額の単位（`auto`／`百万円`／`億円`） |
| `debt_includes_lease` | `false` | 有利子負債にリース負債を含めるか |

環境変数 `FRA_DRAWIO_ARGS` で draw.io に追加の引数を渡せます（例：Linux の CI では `--no-sandbox`）。

## 作成されるファイル

```text
<Vault>/決算分析/
├── 企業一覧.md
└── 村田製作所(6981)/
    ├── 企業概要.md
    ├── 2027年3月期_第2四半期.md      # 決算ノート（A. 3分でわかる決算概要 〜 H. まとめ、出典）
    ├── data/2027年3月期_第2四半期.json  # 決算データ（出典の頁つき）
    └── assets/
        ├── 2027年3月期_第2四半期_PL.drawio / .svg
        ├── 2027年3月期_第2四半期_BS.drawio / .svg
        ├── 2027年3月期_第2四半期_CF.drawio / .svg   # CF 計算書がない四半期は作らない
        └── 2027年3月期_第2四半期_SEG.drawio / .svg  # セグメントまたは売上内訳が2つ以上ある場合
```

## リポジトリの構成

| パス | 内容 |
|---|---|
| `SKILL.md` | スキルの定義と手順（Claude Code が読む） |
| `references/` | 分析の基準、情報収集と出典のルール、データ形式、ノートの構成、図のルール |
| `scripts/` | 取得（`tdnet.py`・`fetch.py`・`edinet.py`・`browser_fetch.cjs`）、解析（`xbrl_facts.py`・`xbrl_to_data.py`・`pdf_text.py`）、計算と検証（`fin.py`）、図（`diagrams.py`）、Obsidian（`obsidian.py`）、最終チェック（`check_report.py`） |
| `tests/` | テスト（`run_tests.py`）とサンプルデータ。テストは一時フォルダの Vault と設定を使い、利用者の Vault と設定は変更しない |
| `justfile` | `just test`・`just lint`・`just ci` など |

## テスト

```bash
just test        # テスト一式
just ci          # lint と test を並列に実行
just test-live   # TDnet への接続テストを含める
```

実企業の XBRL を使うテストは、`~/.cache/financial-report-analyzer/` にその資料がない場合はスキップされます。

## 注意事項

- 作成されるノートは決算の内容を理解するための資料で、特定の銘柄の売買を勧めるものではありません。
- 取得した決算資料の著作権は各社にあります。資料はローカルのキャッシュに保存するだけで、このリポジトリには含めていません。
- 各サイト（TDnet・JPX・企業 IR サイト・EDINET）の利用規約に従って使ってください。
- 不明な数値は推測で補わず、出典のない業績数値や経営者の発言は書かない設計です。ただし、AI が作成した文章である点に留意し、重要な数値は出典の資料で確認してください。
