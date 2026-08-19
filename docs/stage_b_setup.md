# Stage B (HunFlair2) — 実機 GPU セットアップ & 検証手順

## 修正履歴 (重要)

初版でモジュール名衝突 (`ImportError: cannot import name 'JATSParser' from 'preprocess'`) が出た。
原因は conda 環境内に別の `preprocess` パッケージが入っており、フラットな
`from preprocess import ...` だと site-packages 側が優先されていた。

→ `src/pmc_annotator/` パッケージ化 + `pip install -e .` で **完全に名前空間を分離** した。

## 1. 環境準備

```bash
# 推奨: 専用の conda/mamba 環境 (flair は Python 3.9+)
mamba create -n hunflair python=3.11 -y
mamba activate hunflair

# 本パッケージ + 依存をまとめてインストール
cd /path/to/pmc_annotator
pip install -e ".[hunflair]"      # flair, scispacy, lxml が入る

# scispacy モデル (SciSpacySentenceSplitter 用)
pip install https://s3-us-west-2.amazonaws.com/ai2-s2-scispacy/releases/v0.5.4/en_core_sci_sm-0.5.4.tar.gz

# (推奨) 略語解決。HunFlair2 linker の精度に効く。OS依存で入らない場合あり
pip install pyab3p   # 失敗しても致命的ではない (verify スクリプトで AVAILABLE/NOT FOUND を表示)
```

GPU の確認:
```bash
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.device_count())"
```

## 2. 動作確認 (まず NER のみ・高速)

linker はオントロジー前処理に時間がかかるので、最初は NER だけで配線を確認:

```bash
# どのディレクトリからでもOK (pip install -e . 済みなので)
python tests/verify_hunflair.py --device cuda:0 --no-linkers

# あるいは:
python -m pmc_annotator.verify_hunflair --device cuda:0 --no-linkers
```

確認ポイント:
- `[4]` でエンティティタイプ分布が出るか (gene/disease/chemical/species/cell_line)
- `[6]` でオフセット整合性 **NG=0** か ← ここが最重要

> オフセット計算ロジック (`passage.offset + sentence.start_position + span.start_position`)
> は flair 0.15.1 で start_position が「渡したテキスト内の絶対文字位置」を返すことを
> 検証済み。NG が出る場合は scispacy splitter のバージョン差を疑う。

## 3. linker 込みの本確認

```bash
python tests/verify_hunflair.py --device cuda:0
```

初回は CTD / NCBI Gene / NCBI Taxonomy のオントロジー前処理が走るので
数分〜十数分かかる (2回目以降はキャッシュされ高速)。

確認ポイント:
- `[4]` で各エンティティの **正規化率** (linked %) が極端に低くないか
- `[5]` で ID フォーマットが想定通りか:
  - gene    → `NCBIGene:7157` 形式
  - species → `NCBITaxon:9606` 形式
  - disease → `MESH:Dxxxxxx` 形式
  - chemical→ `MESH:Dxxxxxx` 形式
  - cell_line → 正規化なし (NER のみ)

## 4. 自前の PMC 論文で確認

```bash
python tests/verify_hunflair.py --device cuda:0 --xml /path/to/PMC1234567.xml
```

## 5. パイプラインを実行

```bash
# Stage A だけ
python -m pmc_annotator.pipeline preprocess \
    --input /path/to/PMC_OA_xml \
    --output ./data/intermediate \
    --shard-size 500 --workers 16

# Stage B (HunFlair2)
python -m pmc_annotator.pipeline annotate \
    --input ./data/intermediate \
    --output ./data/output \
    --device cuda:0
```

## 6. 既知の注意点

| 項目 | 内容 | 対処 |
|---|---|---|
| **モジュール名衝突** | `preprocess` という名前は scispacy 系の依存パッケージにも存在し、フラット import すると site-packages 側が優先されることがある | `pip install -e .` でパッケージ化済み (本リポジトリでは解決済み) |
| pyab3p | 略語解決ライブラリ。未導入だと "FXS" 等の正規化精度が落ちる | `pip install pyab3p`、ダメなら諦めて後段補完 |
| gene-linker | **Human 限定** | 非ヒト遺伝子は別途 (種情報と組み合わせて TogoID で補完) |
| GPU メモリ | 複数 linker を同時ロードすると VRAM を食う | linker をエンティティ別にプロセス分離、または `--no-linkers` で二段構え |
| splitter 初回 | scispacy モデルDLが必要 | `en_core_sci_sm` を事前 DL |
| 文の最大長 | 極端に長い文 (表を平文化した等) でメモリ急増 | preprocess 側で表本文を除外済 (`include_tables_text: false`) |

## 7. 次のステップ

実機でオフセット NG=0・正規化率が妥当と確認できたら:
- shard 並列 (`pipeline.py annotate` を GPU 別プロセスで)
- PubDictAnnotator / RegexAnnotator の追加 (同じ `__call__` インタフェース)
- TogoID 正規化レイヤー (gene-linker の Human 限定を補完する意味でも重要)
