---
name: financial-report-analyzer
description: 上場企業の決算資料（決算短信・決算説明資料・有価証券報告書）を一次資料から取得・解析し、財務三表（PL・BS・CF）を数値に比例した draw.io 図（.drawio と SVG）で図解し、ビジネスモデルと7つの観点（ビジネスモデル・収益性・成長性・安全性・効率性・将来性・リスク）を【事実】【解釈】【仮説】に分けて分析したノートを Obsidian の「決算分析」フォルダに蓄積する。企業名・証券コード・決算資料URL・PDF・対象決算期・「最新決算」から起動し、同じ決算期は重複させず更新、過去決算との比較も行う。
when_to_use: 「〇〇の決算を分析して」「7974 の最新決算をまとめて」「この決算短信PDFを読んで」「財務三表を図解して」「決算をObsidianに記録して」「前回決算から何が変わった？」など、上場企業の決算・財務諸表の分析を依頼されたとき。/financial-report-analyzer で明示的にも実行できる。
argument-hint: "[企業名|証券コード|資料URL|PDFパス] [対象決算期（省略時は最新）]"
allowed-tools:
  - Bash(python3 -I ${CLAUDE_SKILL_DIR}/scripts/*)
  - Bash(pdftotext *)
  - Bash(pdfinfo *)
  - Bash(node ${CLAUDE_SKILL_DIR}/scripts/browser_fetch.cjs *)
  - WebSearch
  - WebFetch
---

# financial-report-analyzer — 決算分析ノートの作成

入力：$ARGUMENTS

目的は決算書を短く要約することではなく、**会計知識が発展途上の読者でも、企業がどう売上と利益を生み、資金をどう集めて使い、どんな強みとリスクを持つかを理解できる資料**を作ること。説明は平易に、分析は専門的かつ論理的に行う。投資判断を断定する文書にはしない。

着手前に読むもの（必要な箇所だけでよい）:
- 分析の基準：[references/analysis-guide.md](references/analysis-guide.md)（3階層、7観点、業種別指標、会計基準・期間の注意）
- 情報収集と出典：[references/sources.md](references/sources.md)
- データ形式：[references/data-schema.md](references/data-schema.md)
- ノートの構成：[references/report-template.md](references/report-template.md)
- 図のルール：[references/diagram-rules.md](references/diagram-rules.md)

スクリプトは `${CLAUDE_SKILL_DIR}/scripts/` にあり、すべて標準ライブラリだけで動く（PDF は poppler の pdftotext、図の書き出しは draw.io Desktop）。
実行は `python3 -I ${CLAUDE_SKILL_DIR}/scripts/<name>.py …`。出力は JSON。以下 `S=${CLAUDE_SKILL_DIR}/scripts` と書く。

## 絶対に守ること

- 数値は一次資料から取る。**不明な数値を推測で埋めない。取得できない値は null（未開示）にし、0 で代用しない。**
- 出典のない業績数値・会社予想・経営者の発言を書かない。数値と重要な主張には `［S1 p.4］` の形で出典を付ける。
- 計算はスクリプト（fin.py）で行う。暗算した数値を本文に書かない。
- 連結と単体、累計と単独四半期、異なる会計基準の数値を断りなく混ぜない。
- CF 計算書がない期は CF を作らない。図も作らない。
- 図は diagrams.py で数値から生成する（画像生成 AI で代替しない）。
- 利用者が追記したメモを消さない（obsidian.py upsert が自動生成部分だけを更新する。中止されたら従う）。
- サンプルデータを使うときは `sample: true` にし、実企業の分析と混同させない。

## 手順

### 0. 設定の確認（初回のみ設定が必要）

```bash
python3 -I $S/obsidian.py detect
```
- `configured_vault` があればそのまま使う（毎回は聞かない）。
- 無ければ `python3 -I $S/obsidian.py setup` を実行。Vault が1つなら自動で設定される。終了コード 3（複数候補・候補なし）のときだけ、候補を示して利用者に Vault のパスを確認し `setup --vault <path>` を実行する。
- 設定ファイル：`~/.config/financial-report-analyzer/config.json`（vault_path, root_folder=決算分析, embed_width, display_unit, debt_includes_lease など）。

### 1. 企業と対象期間を確定する

- 証券コードがあればそれを使う。企業名だけなら `python3 -I $S/tdnet.py search --q <社名>` や WebSearch（「社名 証券コード IR」）で特定し、決算短信の表紙（XBRL の CompanyName）で**公式な商号**を確認する。
- URL・PDF が渡されたらそれを主資料にする（PDF は `~/Downloads` 等にあるものをそのまま読む）。
- 期間指定があればそれを優先。なければ「今日時点で公表済みの最新の決算短信」。
- 既存ノートを確認：
  ```bash
  python3 -I $S/obsidian.py paths --code <コード> --name <商号> --period <YYYY年M月期_本決算|第N四半期>
  ```
  `note_exists` / `data_exists` が true なら**再分析**。既存ノートとデータを読み、新しいファイルを作らず更新する（下の 8 で upsert）。`existing_periods` に過去の決算があれば手順 6 で比較する。

### 2. 資料を集める（[sources.md](references/sources.md)）

- 直近約1か月の開示：`python3 -I $S/tdnet.py search --code <コード> --title "決算短信|説明|補足"` → `tdnet.py download --url <pdf/xbrl> --code <コード> --period <ラベル>`。
- それより古い資料：企業 IR サイトの IR ライブラリを WebFetch して PDF の URL を見つけ、`fetch.py <URL> --code … --period …`。有報・半期報告書は IR サイトの PDF か、`EDINET_API_KEY` があれば `edinet.py`。
- 最低限そろえる：決算短信（数値の一次資料）。可能なら決算説明資料（事業の説明・KPI）、有報（事業内容・リスク・セグメント詳細）、中期経営計画。
- 各資料の「資料名・URL・公表日・対象期間・会計基準・連結/単体・単位」を確認する。訂正開示があれば訂正後を使う。

### 3. 本文と数値を読む

- `python3 -I $S/pdf_text.py <pdf> --out <scratch>/s1.txt`（頁マーカー付き）→ 必要な頁を Read。`--grep "セグメント|キャッシュ"` で場所を探す。
- XBRL がある場合：`python3 -I $S/xbrl_facts.py <zip> --statement pl|bs|cf|summary --format table|long` で値を確認できる。

### 4. 決算データ JSON を作る（[data-schema.md](references/data-schema.md)）

- まず企業フォルダを用意：`python3 -I $S/obsidian.py init-company --code <コード> --name <商号> --industry <業種>`（企業一覧も更新される）。
- XBRL がある場合は下書きを自動作成し、レビューする：
  ```bash
  python3 -I $S/xbrl_to_data.py <xbrl.zip> --pdf-url <短信PDFのURL> --xbrl-url <zipのURL> --out <data_dir>/<ラベル>.json
  ```
  レビュー項目（`review_todo` に列挙される）：業種、各行の `src` に頁番号、`kind: unknown` の分類、BS の `highlight`・有利子負債・`no_debt`、
  セグメント（会社開示のセグメント利益率を `fin.py calc` で再計算し、分母が外部顧客向け売上かセグメント売上計かを確かめてから `revenue_total` の要否を決める）、
  XBRL に値がない欄（PDF で「－」なら `nil_fields`、空欄なら null のまま）、会社予想（修正があれば `forecast.previous`）、本文で使う KPI（`extra`）、一時的な損益の `one_off`、`cf.cash_reconciliation_note`、`sources` の追加、最後に `draft: false`。
- XBRL がない場合（IR サイトの PDF など）は、PDF の表から同じ形式で手入力する。転記後は必ず次の validate で照合する。
- 業種特有の科目は名称を変えずに入れる。

### 5. 検証する

```bash
python3 -I $S/fin.py validate <data.json>
```
- `errors` が空になるまで直す。**合わない数値を調整して合わせない。** 原資料を読み直し、転記ミス・行の分類・内訳の重複を確認する。
- `rounding_diffs`（丸め誤差）は問題ない。本文の注記で「端数処理により合計が一致しない場合がある」と書く。
- 資料間の不一致は sources.md の手順で原因を調べて `notes` に書く。

### 6. 指標・表・履歴比較

```bash
python3 -I $S/fin.py metrics <data.json>          # 指標（定義つき）
python3 -I $S/fin.py tables  <data.json>          # 本文に貼る表（PL/BS/CF/指標/セグメント/会社予想）
python3 -I $S/fin.py history <data_dir>           # 過去の決算がある場合：推移・予想と実績の差・比較可能性
python3 -I $S/fin.py quarter <当期.json> <前四半期.json>   # 単独四半期が必要な場合
```
`tables` の出力（`<!-- fra:table:… -->` ブロック）は**そのまま**本文に貼る（check_report.py が再生成結果と照合する）。
本文で使う計算（差額・比率・億円換算など）も暗算しない：`python3 -I $S/fin.py calc "(814570-539804)/100" --places 0`。
一時的な損益（`one_off: true`）がある場合、metrics が「一時要因を除く」段階利益（参考）を出す。

### 7. 図を作って確認する

```bash
python3 -I $S/diagrams.py build <data.json> --out-dir <company_dir>/assets --prefix <ラベル>
```
- `ok: true` と各図の `verify.ok` を確認。失敗時は [diagram-rules.md](references/diagram-rules.md) の直し方に従う。
- 目視確認：`--out-dir <scratchpad>` と `--png` で別に出力し、PNG を Read で見る（文字切れ・重なり・余白）。確認後、本番の assets に出力する（PNG は Vault に置かない）。
- CF 未開示の期は `skipped` に理由が出る。
- セグメントが2つ以上（単一セグメントなら data の `revenue_breakdown` が2つ以上）あると補助図 SEG（売上高と利益の構成比の円グラフ、金額・構成比・利益率の表付き）も作られる。ビジネスモデルの章に埋め込む。

### 8. ノートを書く（[report-template.md](references/report-template.md)、[analysis-guide.md](references/analysis-guide.md)）

1. 図の埋め込みコードを取得：`python3 -I $S/obsidian.py embeds --code <コード> --period <ラベル> [--kind PL]`（章ごとに `--kind` で1つずつ取り出せる）
2. テンプレートに従って本文（body.md）と frontmatter（fm.json）を scratchpad に書く。
   - 冒頭「3分でわかる決算概要」は数値の羅列ではなく「なぜそうなったか」。
   - 【事実】【解釈】【仮説】を区別し、重要な解釈・仮説には確信度、仮説には検証方法を書く。情報不足は「判断材料不足」。
   - 専門用語・業種特有の科目は初出で説明する。
   - 過去ノートがあれば「前回決算からの変化」を書く（比較できない数値は比較しない）。
   - data JSON の `watch_next`（次回確認する指標）と `concerns`（懸念点）も更新しておく（次回の比較で使う）。
3. 書き込み：
   ```bash
   python3 -I $S/obsidian.py upsert --note <company_dir>/<ラベル>.md --body body.md --frontmatter fm.json
   ```
   - 終了コード 4（`aborted_manual_edit`）＝自動生成部分が人手で編集されている。上書きせず、利用者に知らせて方針を確認する（追記をメモ欄に移す、または利用者の了承後に `--force`。--force でも元のファイルはバックアップされる）。
4. 企業概要を更新：**既存の企業概要を Read して内容を引き継ぎ**、本文（テンプレート3）を書き、`upsert --note <company_dir>/企業概要.md` で更新（frontmatter は 会社名・証券コード・業種・tags・`fra_type: company_overview`）。
5. 相互リンクと一覧を再生成：`python3 -I $S/obsidian.py relink --code <コード>`
6. データ JSON を後から直した場合や、スクリプトの更新で表の形式が変わった場合は、本文を書き直さずに表だけを更新できる：`python3 -I $S/obsidian.py refresh-tables --note <ノート.md> --data <data.json>`（図は diagrams.py build で作り直す）。

### 9. 最終チェック

```bash
python3 -I $S/check_report.py --note <ノート.md> --data <data.json> --vault <Vault>
```
- `errors` をすべて直す（表の不一致、図の未埋め込み、必須の章、出典 URL の記載漏れ、draft のまま等）。
- `warnings` の「本文の数値がデータと一致しない」は1件ずつ確認する。照合は「同じ文に出てくる科目名」の値で行うので、数値の主語（科目名）を同じ文に書く。誤記なら本文を直す。資料にある別の数値（KPI など）なら出典つきで data JSON の `extra` に登録し（本文の書き方と名前が違えば `aliases`）、図と表を作り直してから再チェックする。数値の例示など照合が不要な文は `<!-- fra:nocheck -->…<!-- /fra:nocheck -->` で囲む。

### 10. 報告

利用者には、作成・更新したファイル（ノート、図、データ）、決算の要点3つ、判断材料が不足している点、検証結果（丸め誤差以外の不一致がないこと）を簡潔に伝える。

## トラブル時

- draw.io Desktop がない：`brew install --cask drawio`（利用者の承認を得てから）。設定 `drawio_cli` でパス指定も可。
- pdftotext がない：`brew install poppler`。
- TDnet で見つからない：1か月より古い可能性。日本取引所グループのサイト（www2.jpx.co.jp/disc/<5桁コード>/）に同じ開示文書（PDF・XBRL）が残っていることが多い（探し方は references/sources.md）。なければ IR サイトか EDINET を使う。
- IR サイトが WebFetch・curl を拒否する（HTTP 403 など）：`node $S/browser_fetch.cjs list <ページURL>` で PDF リンクを列挙し、`node $S/browser_fetch.cjs download <ページURL> <空の専用ディレクトリ> <PDFのURL>...` で取得する。画面付きの Chromium（Playwright のキャッシュ）を使うため、実行中にウィンドウが表示される。playwright-core か Chromium が無ければ（終了コード 3）、インストールは利用者の承認を得てから行うか、利用者に PDF を保存してもらう。
- EDINET API キーがない：IR サイトの PDF を使う。キー登録は利用者が行う（https://api.edinet-fsa.go.jp/）。
- テストは `just -f ${CLAUDE_SKILL_DIR}/justfile test`。テスト出力はスクラッチ領域の一時 Vault に書き、利用者の Vault を変更しない。
