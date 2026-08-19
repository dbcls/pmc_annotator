# PMC Annotator — MVP

PMC OA サブセットの JATS XML を入力に、PubTator Central 相当（＋拡張）の
網羅的アノテーションを生成するパイプラインの **MVP スキャフォルド**。

## アーキテクチャ

```
PMC OA XML (数百万件)
  ↓ [Stage A] 前処理: JATS → BioC-JSON shard (CPU並列, lxml + multiprocessing)
  ↓ [Stage B] HunFlair2: gene/disease/chemical/species (GPU推論)
  ↓ [Stage C] PubDictionaries: GO/Reactome/UniProt名 など (Aho-Corasick)    ← 後段
  ↓ [Stage D] 正規表現: UniProt AC / PDB / GEO / SRA / ChEMBL / DOI など   ← 後段
  ↓ [Stage E] TogoID 正規化: ID 相互変換 + 統合スキーマ                       ← 後段
  ↓
BioC-JSON (主出力) + RDF/SPARQL (オプション)
```

各 stage は **shard 単位で冪等**。`.done` マーカで再実行時にスキップ。
途中で失敗しても該当 shard だけ再実行すればよい。

## 現状の実装範囲 (MVP)

- ✅ Stage A 前処理 (`preprocess.py`)
  - JATS XML → セクション付き Passage (`title`/`abstract`/`introduction`/`methods`/`results`/`discussion`/`fig_caption`/`table_caption`)
  - 文書全体オフセットの厳密保持
  - PMCID / PMID / DOI / journal / year の取得
  - 図表キャプションを別 passage として infon 付きで保持
  - multiprocessing による CPU 並列、gzip JSONL shard 出力
- ✅ Stage B HunFlair2 アノテーター (`annotate_hunflair.py`)
  - スキャフォルドのみ (GPU 環境がある実機で動作確認が必要)
  - EntityLinker (NEN) 込みで gene / disease / chemical / species
- ✅ I/O ヘルパー (`io_utils.py`) — shard 読み書き、`.done` マーカ
- ✅ オーケストレーター CLI (`pipeline.py`)
- ✅ 前処理のユニットテスト (実機で `python tests/test_preprocess.py`)

## 使い方

### Stage A だけ (前処理)
```bash
cd src
python pipeline.py preprocess \
    --input /path/to/PMC_OA_xml \
    --output ./data/intermediate \
    --shard-size 500 \
    --workers 16
```

### Stage B (HunFlair2 アノテーション)
```bash
# 別 GPU で複数並列を回したい場合は、shard を分けて別プロセスで起動
python pipeline.py annotate \
    --input ./data/intermediate \
    --output ./data/output \
    --device cuda:0
```

### A + B 一気通貫
```bash
python pipeline.py all \
    --input /path/to/PMC_OA_xml \
    --intermediate ./data/intermediate \
    --output ./data/output
```

## 環境準備

```bash
# CPU 系
pip install lxml pyyaml

# Stage B (HunFlair2)
pip install flair scispacy
# 初回起動時にモデルを HuggingFace から自動DL
```

## 後段への拡張ポイント

`annotate_hunflair.py` の `HunFlairAnnotator` と同じインタフェース
(`__call__(Document) -> Document`) で以下を実装し、`pipeline.py` で
順次適用するだけで拡張可能：

- `PubDictAnnotator`: 辞書を Aho-Corasick に展開して全文マッチ
- `RegexAnnotator`: configs/default.yaml の正規表現群を一括適用
- `TogoIDNormalizer`: 抽出済み ID を `togoid_convertId` でバルク変換

`Annotation.source` フィールドで出所を区別できるので、後段で
出所別の信頼度重みづけや重複排除が可能。

## OA 全体を回す際の運用メモ

- **shard 並列**: GPU 4枚あれば、shard を 4 グループに分けて `--device cuda:N` を
  指定したワーカを 4 つ並走させる。`.done` マーカで取り合いになるので、
  shard ID を担当範囲で割り振るのが安全。
- **ジョブキュー**: Celery / RQ / Airflow を使うなら、`stage_annotate` の
  shard ループを 1 タスク = 1 shard に分解。
- **中間ストレージ**: Parquet に寄せると DuckDB / ClickHouse から横断クエリ可能。
  RDF/SPARQL 化するなら別途 BioC-JSON → Turtle 変換器を追加。
- **増分更新**: PMC は毎日更新。`stage_preprocess` を差分ディレクトリに対して
  別 shard 名前空間で回せば既存 shard を壊さず追加できる。

---

## Repository layout

```
src/pmc_annotator/   installable package (extraction → verification → metrics → role classification)
analysis/            one-off analyses & evaluations (EPMC/TogoID coverage diff, Data Citation Corpus eval)
scripts/             orchestration runners (run_oa_pipeline.py)
tests/               unit tests (+ tests/diagnostics/ for ad-hoc diagnostic scripts)
archive/             superseded modules — kept for reference, NOT wired into the pipeline (see REORG_REPORT.md)
configs/, data/      config + small dictionaries + a shard_plan example (bulk data is git-ignored)
docs/                HANDOFF (JA/EN), REFERENCES, stage-B setup
```

## Documentation
- `docs/HANDOFF_EN.md` — project introduction (for external collaborators).
- `docs/HANDOFF.md` — same, in Japanese.
- `docs/REFERENCES.md` — related work + comparison table.
- `docs/stage_b_setup.md` — HunFlair2 (GPU) setup notes.
- `REORG_REPORT.md` — what was moved where during the repo cleanup, and open decisions to confirm.

## Install
```bash
pip install -e .            # core stages
pip install -e ".[hunflair]"  # + concept layer (GPU box)
```
