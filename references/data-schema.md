# 決算データ JSON の仕様（schema_version 1）

1つの決算期につき1ファイル。保存先は `<Vault>/決算分析/<会社名>(<コード>)/data/<決算期ラベル>.json`。
図・表・指標・履歴比較はすべてこのファイルから生成する。**本文に書く数値もこのファイルに入っている値（または fin.py の計算値）だけを使う。**

## 原則

- 金額は `basis.unit`（原則として開示資料の単位。多くは百万円）の**整数または小数**で、開示値をそのまま入れる。換算・丸めをしない。
- 取得できない値は `null`。**0 で代用しない。**
- 財務諸表で「－」（その期に該当する金額がない）と表示された欄は、`value`（または `prior`）を `null` にし、`"nil_fields": ["value"]` を付ける。
  計算では 0、表では「－」として扱われる（XBRL の下書きでは自動で付く）。「未開示」とは区別する。
- 符号: 費用は正の数で入れ、`kind` で引き算を表す。会社の表示で費用がマイナス（例：法人税等調整額の戻入）なら負の数で入れる。
- `prior` は比較対象（PL・CF は前年同期／前期、BS は前期末）。比較対象が組替え後の値なら組替え後を使い、`notes` に書く。
- `src` は `"S1 p.7"` の形式（sources の id ＋頁番号）。**頁番号は PDF の物理ページ番号（PDF ビューアの表示、`pdf_text.py` の `=== p.N ===`）を使う。** 決算短信は添付資料に印字された頁番号と2程度ずれるので、印字頁は使わない。
- XBRL から作った下書き（`xbrl_to_data.py`）は `draft: true`。確認を終えたら `false` にする（check_report.py が確認する）。

## トップレベル

| キー | 型 | 内容 |
|---|---|---|
| `schema_version` | 1 | |
| `sample` | bool | テスト用サンプルなら true。図とノートに「サンプル」と表示される |
| `draft` | bool | 下書き。レビュー後 false |
| `company` | object | `name`（公式商号。例：任天堂株式会社）、`short_name`（図の見出し用。省略可）、`code`（証券コード4桁）、`industry`（東証33業種）、`fiscal_year_end_month`、`url` |
| `period` | object | 下表 |
| `basis` | object | `standard`（`J-GAAP` / `IFRS` / `US-GAAP` / `JMIS`）、`scope`（`連結` / `単体`）、`unit`（`円` / `千円` / `百万円`）、`rounding`（例：百万円未満切捨て） |
| `sources` | list | `{id, title, url, published, kind, pages?, sha256?, xbrl_url?}`。kind は 決算短信 / 決算説明資料 / 有価証券報告書 / 半期報告書 / 適時開示 / IRサイト / 統計 |
| `pl` | object | 損益計算書 |
| `bs` | object | 貸借対照表（財政状態計算書） |
| `cf` | object | キャッシュ・フロー計算書 |
| `segments` | list | セグメント情報 |
| `forecast` | object\|null | 会社予想（この資料の時点） |
| `extra` | list | 本文で引用するその他の数値（販売台数、受注残、KPI など）。`{label, value 又は values, unit, src}` |
| `one_off` 等の注記 | | PL 行の `one_off: true` で一時的な損益を示す |
| `notes` | list | 会計方針の変更、遡及修正、決算期変更などの注記 |
| `watch_next` | list | 次回決算で確認する指標（履歴比較で使う） |
| `concerns` | list | 今回の懸念点（次回、改善したか確認する） |

### period

| キー | 例 | 内容 |
|---|---|---|
| `label` | `2027年3月期_第2四半期` | ファイル名と同じ。種類は 本決算 / 第1四半期 / 第2四半期 / 第3四半期 |
| `fiscal_year` | `2027年3月期` | |
| `type` | `FY` / `Q1` / `Q2` / `Q3` | |
| `start` / `end` | `2026-04-01` / `2026-09-30` | **累計期間**の開始日・終了日 |
| `months` | 6 | 累計の月数（年換算に使う） |
| `is_cumulative` | true | 決算短信の四半期は累計値 |
| `published` | `2026-11-05` | 資料公表日 |
| `irregular` | false | 決算期変更などの変則期間なら true |
| `restated` | false | 前期数値の遡及修正があれば true（単独四半期の差分算出を止める） |
| `no_comparative_reason` | `初めての連結決算のため…` | 前期・前年同期の比較値が開示されていない理由（任意）。全行の `prior` が null のとき、表の前期欄は「―」になり、この理由が表の注記に入る。会社が説明資料で参考値（単体の前年同期など）を示していれば、`extra` に「参考値」と明記して入れる |

## pl

```json
"pl": {
  "revenue_key": "revenue",            // 構成比の分母になる行
  "split_after": "operating_income",   // PL 図①の終点（銀行は ordinary_income など）
  "net_income_key": "net_income_parent",
  "lines": [
    {"key": "revenue", "label": "売上高", "kind": "revenue", "value": 966041, "prior": 1099565, "src": "S1 p.6"},
    {"key": "cogs", "label": "売上原価", "kind": "expense", ...},
    {"key": "gross_profit", "label": "売上総利益", "kind": "subtotal", ...},
    {"label": "受取利息", "kind": "income", "parent": "non_op_income", ...},   // 内訳行
    {"key": "non_op_income", "label": "営業外収益合計", "kind": "income", ...}
  ]
}
```

非連結（単体）の会社は「親会社株主に帰属する」行がないため、`net_income_key` は `"net_income"`（当期純利益の行）にし、
会社予想の当期純利益は `forecast.net_income` に入れる（xbrl_to_data.py が自動で設定する）。
財務諸表が千円単位の会社は `basis.unit` を `"千円"` にし、予想などサマリーの金額も千円に換算して入れる（同じく自動）。
本文で会社資料の百万円表示（百万円未満切捨て）の値を引用してよい（check_report.py は切捨て・四捨五入のどちらも一致とみなす）。

`kind`:

| kind | 意味 | 計算上の扱い |
|---|---|---|
| `revenue` | 売上高・営業収益・経常収益 | ＋ |
| `income` | 営業外収益・特別利益・金融収益・その他の収益・持分法損益 | ＋ |
| `expense` | 売上原価・販管費・営業外費用・特別損失・法人税等 | − |
| `attribution` | 非支配株主に帰属する利益 | − |
| `subtotal` | 段階利益（売上総利益・営業利益・経常利益・税引前利益・当期純利益・親会社帰属利益） | 直前までの累計と照合 |
| `info` | 計算に使わない参考行（包括利益など）。表に出すときは `show: true` | なし |
| `unknown` | 下書きで分類できなかった行。**残っていると validate がエラー** | |

- `parent` を持つ行は内訳。段階利益の連鎖計算から除外され、「親 = 内訳の合計」を照合する。
- 業種や会計基準で成立しない照合は、その subtotal に `"check": false` を付けて止める（理由を `notes` に書く）。例：IFRS で営業利益を表示しない会社が独自の「事業利益」を開示している場合。

標準キー（指標計算で使う）: `revenue`, `cogs`, `gross_profit`, `sga`, `operating_income`, `non_op_income`, `non_op_expense`,
`ordinary_income`, `extraordinary_income`, `extraordinary_loss`, `pretax_income`, `income_taxes`, `net_income`, `nci`,
`net_income_parent`, `other_income`, `other_expense`, `finance_income`, `finance_costs`, `equity_method`, `interest_expense`（内訳）。

## bs

```json
"bs": {
  "date": "2026-09-30", "prior_date": "2026-03-31",
  "assets":      [{"group": "流動資産", "key": "cash", "label": "現金及び預金", "value": ..., "prior": ..., "src": "S1 p.5", "highlight": true}, ...],
  "liabilities": [{"group": "流動負債", "label": "短期借入金", "value": ..., "interest_bearing": true}, ...],
  "equity":      [{"group": "純資産", "key": "shareholders_equity", "label": "株主資本合計", ...},
                  {"group": "純資産", "label": "利益剰余金", "parent": "shareholders_equity", ...}],
  "group_totals": {"流動資産": {"value": ..., "prior": ...}, "固定資産": {...}, "流動負債": {...}, "固定負債": {...}, "純資産": {...}},
  "totals": {"assets": {...}, "current_assets": {...}, "current_liabilities": {...}, "liabilities": {...},
             "net_assets": {...}, "liabilities_and_net_assets": {...}},
  "owners_equity": {"label": "自己資本", "value": ..., "prior": ..., "definition": "純資産 − 新株予約権 − 非支配株主持分"},
  "no_debt": false,
  "debt_breakdown": []
}
```

- `group` ごとに、`parent` のない行の合計が `group_totals` と一致すること（丸め誤差は許容）。
- `key` の標準: `cash`, `receivables`, `inventories`, `payables`, `ppe`, `intangibles`, `goodwill`, `investment_securities`。
- `interest_bearing: true` … 有利子負債（借入金・社債・CP・リース負債）。リース負債には `lease: true` も付ける（既定の設定では有利子負債合計から除外して別記）。
- 有利子負債が「その他」に含まれていて BS 本表に出ない場合は、注記の金額を `debt_breakdown: [{label, value, prior, src}]` に入れる。
- 無借金の会社は `no_debt: true`（有利子負債 0 と判断した根拠を notes に書く）。
- `highlight: true` … 図で太枠にする重要科目（本文で取り上げる科目）。
- 業種特有の科目（例：販売金融債権、営業貸付金、販売用不動産、契約資産、保険契約負債）は**そのままの名称**で行を作り、本文で意味を説明する。
- 銀行・保険など流動／固定の区分がない BS は、group を「資産」「負債」「純資産」の1つずつにしてよい。

## cf

```json
"cf": {
  "disclosed": true,
  "cash_definition": "現金及び現金同等物",
  "begin": {"label": "現金及び現金同等物の期首残高", "value": ..., "prior": ..., "src": "..."},
  "end":   {"label": "現金及び現金同等物の期末残高", "value": ..., "prior": ..., "src": "..."},
  "lines": [
    {"key": "cfo", "label": "営業活動によるキャッシュ・フロー", "value": ..., "prior": ...},
    {"label": "減価償却費", "parent": "cfo", "value": ...},
    {"key": "cfi", ...}, {"key": "cff", ...},
    {"key": "fx", "label": "現金及び現金同等物に係る換算差額", ...},
    {"key": "scope_change", "label": "連結の範囲の変更に伴う現金及び現金同等物の増減額", ...}
  ],
  "capex": {"label": "有形・無形固定資産の取得による支出", "value": ..., "prior": ...},
  "cash_reconciliation_note": "BS の現金及び預金との差は預入期間3か月超の定期預金"
}
```

- `parent` のない行（営業・投資・財務・換算差額・その他の増減）の合計 ＋ 期首 ＝ 期末 を照合する。
- 内訳行（`parent: "cfo"` など）の合計が区分の合計と一致するかも照合する。営業 CF の「小計」行は `"kind": "subtotal"` を付けて合計から除く。内訳を一部しか入れない場合は親に `"check_children": false`。
- **CF 計算書がない四半期は `{"disclosed": false, "reason_if_not": "..."}` だけにする。** 数値を作らない。
- 期首・期末は同じ定義（現金及び現金同等物）を使う。BS の「現金及び預金」とは一致しないことがあるので、差があれば `cash_reconciliation_note` に理由を書く。

## segments

```json
[{"name": "ゲーム専用機", "revenue": ..., "revenue_prior": ..., "profit": ..., "profit_prior": ..., "src": "...",
  "revenue_basis": "外部顧客への売上高", "assets": null}]
```

単一セグメントの会社は空配列にし、代わりに地域別・製品別の売上を `extra` に入れる。
`profit_basis`（例：「セグメント利益（調整額控除前）」）を最初の要素に書くと、図の脚注に使われる。
セグメント利益の合計を連結の営業利益以外（日本基準で「経常利益と調整を行っている」会社など）と照合している場合は、最初の要素に `"profit_reconciles_to": "ordinary_income"`（PL 行の key）を書く。調整額は `"adjustment"`（当期の値）に入れると、図の脚注に「合計＋調整額＝連結の段階利益」と書かれる（差が端数の範囲を超えると書かない）。
`revenue` は外部顧客向け売上（合計が連結売上と一致する値）。利益率は、`revenue_total` があればそれを、なければ `revenue` を分母に計算する。
**会社が開示するセグメント利益率の分母を、`fin.py calc` で再計算して確かめてから決める。**
- 内部売上を含む「セグメント売上計」を分母にしている会社（例：村田製作所）→ `revenue_total` / `revenue_total_prior` を入れる。
- 外部顧客向け売上を分母にしている会社（例：TDK の「売上高比」）→ `revenue_total` を入れない（内部売上の額は必要なら `notes` に書く）。
会社が利益率を開示していない場合は `revenue_total` を入れず、`notes` に「利益率は筆者計算（外部顧客向け売上が分母）」と書く。

## revenue_breakdown（任意）

単一セグメントの会社で、決算説明資料などに製品別・事業別の売上内訳がある場合に入れる。補助図（SEG）を作る。

```json
"revenue_breakdown": [{"label": "ゲーム専用機", "value": 903333, "prior": 1066199, "src": "S2 p.13"},
                      {"label": "IP関連収入等", "value": 62707, "prior": 33365, "src": "S2 p.13"}]
```

内訳が財務諸表と別の単位でしか開示されていない場合（例：財務諸表は千円、説明資料の内訳は百万円未満切捨て）は、
値を換算せずにそのまま入れ、`"revenue_breakdown_unit": "百万円"` を指定する（表はその単位で表示し、図は財務諸表の単位に換算する）。
`check_report.py` は内訳の値（当期・前期・増減・増減率・構成比）を、この単位で換算して本文の数値と照合する。

## extra の別名（aliases）

`check_report.py` は本文の数値を「同じ文に出てくる科目名」の値と照合する。KPI の名前が本文の書き方と違う場合は
`"aliases": ["ゲーム専用機"]` のように別名を付ける（空白の前の部分と「の」の前の部分は自動で別名になる）。

## forecast

```json
{"period": "2027年3月期", "as_of": "2026-11-05", "revenue": 2050000, "operating_income": 370000,
 "ordinary_income": 430000, "net_income_parent": 310000, "eps": 268.9, "dividend": 162,
 "revenue_change_pct": -11.4, "revised": false, "src": "S1 p.1"}
```

本決算では翌期の予想、四半期では当期通期の予想。レンジ予想は `revenue_low` / `revenue_high` を追加する。
会社が業績予想を「未定」とした場合（連結移行・M&A・災害など）は、金額を入れずに `"undetermined": true` と `"undetermined_reason"`（理由と出典）を入れる。配当予想だけがあれば `dividend` は入れる。説明資料の社内予算などは forecast に入れず、出典付きで `extra` に入れる。
過去の予想と実績の比較は `fin.py history` が同じ会計年度の forecast を集めて行う。
