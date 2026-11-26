# Held-out ゴールドの作成手順（役割分類の汎化評価用）

`sample_heldout.py` は、開発に使った gold（`data/gold_standard.md`, 221 件）とは
**ドキュメント単位で素**な held-out 注釈シートを、既存の windows プールから層化抽出する。
再抽出・再 `build_windows` は不要（役割分類と同じ `windows_all.jsonl` から抜く）。

## 前提ファイル（Linux 側）

- `windows_all.jsonl` … `build_windows` の出力（役割分類と同一プール。バケツ 0〜3 を含む）
- `role_prelim_all.tsv` … 同プールに `role_prepass.py` をかけた出力（層化シグナル用）
  ```
  python -m pmc_annotator.role_prepass --windows windows_all.jsonl --out role_prelim_all.tsv
  ```
- `data/gold_standard.md` … 除外する dev gold

## 確定コマンド（200 件版）

```
python sample_heldout.py \
  --windows windows_all.jsonl --prepass role_prelim_all.tsv \
  --exclude-gold data/gold_standard.md \
  --per-class generated=60,used=70,mentioned=70 \
  --dataset-floor refseq_rna=28,refseq_protein=20,geo_series=28,mirbase=12,chebi=8,ensembl_gene=8,tair=10,flybase_gene=6 \
  --dataset-cap geo_series=30,refseq_rna=36,refseq_protein=28,refseq_genomic=22,geo_sample=22,pfam=0,interpro=0,cog=0,prosite=0 \
  --out heldout_annotation.md --key heldout_key.tsv --iaa heldout_iaa.txt
```

得られた構成（seed=20261027）:

```
geo_series 49 · refseq_rna 36 · refseq_protein 28 · geo_sample 22 · refseq_genomic 16
· tair 12 · mirbase 12 · ensembl_gene 10 · chebi 8 · flybase_gene 6 · bioproject 1   (計200)
層: generated 60 / used 70 / mentioned 70   IAA: 36 件
```

## 設計の考え方

- **クラス層化が主**。macro-F1 の不確かさは最小支持クラス（generated）が支配するので、
  総数でなくクラス別 n を設計する。200 版は generated 60 → 95%CI 半幅 ±7.6pt、
  used/mentioned 70 → ±7.0pt 程度。精密な点推定には粗いが、汎化の方向確認には十分。
  （精度を上げるなら 280 版: generated 80 / used 100 / mentioned 100 → generated ±6.6pt）
- **doc 単位で gold と素**（`--exclude-level doc` 既定）。gold の 221 件が属する 207 docs を
  丸ごと除外し、同一文書の別 occurrence の混入（リーク）を防ぐ。
- **データセット網羅**: 希少だが境界が重要な DB（mirbase/chebi/ensembl/tair/flybase）は
  `--dataset-floor` で下限確保。支配的な geo_series/RefSeq 系は `--dataset-cap` で抑制。
  prepass で `rule:mentioned` 確定の自明なドメイン/ファミリ DB（pfam/interpro/cog/prosite）は
  `--dataset-cap <db>=0` で除外。
- **ブラインド**: 予測は出力に載せない（`heldout_key.tsv` は設計者QC用、注釈者には渡さない）。

## オプション要点

- `--per-class gen=..,used=..,mentioned=..` クラス別目標。最低ライン gen50/used70/mentioned70。
- `--dataset-floor <db>=n,..` DB 別下限。pool 不足時は warn（その DB の件数が上限）。
- `--dataset-cap <db>=n,..` DB 別上限。**0 は除外**。クラス枠を埋めきれない時だけソフトに超過。
- `--exclude-level doc|key` 既定 doc（gold の doc_id を丸ごと除外）。
- `--iaa-frac 0.18` 二重注釈に回す割合。`--seed` 再現性。

## 注釈後（採点）

1. `heldout_annotation.md` を `docs/ANNOTATION_GUIDELINE.md` の規約でブラインド注釈
   （規約A: 配列を実際に処理=used / 比較・相同性リストの命名=mentioned、
    規約B: ID が入手材料そのもの=used / 入手物が別物で ID は標的命名のみ=mentioned、
    unsure: 窓だけでは generated/used を排除できないとき）。
2. `heldout_iaa.txt` の 36 件を第二アノテーターにも付けてもらい κ/一致率を測定。
3. 予測は既存 usage_roles 出力に存在（windows_all 全体をカバー）。
   ```
   python eval_roles.py --gold heldout_annotation.md --pred <usage_roles 出力>
   ```
   held-out の doc_id が出力に含まれることを確認（dev とは doc 単位で素）。
4. 得られた macro-F1 が汎化性能。`docs/ROLE_EVALUATION*.md` の「held-out 無し」限界を更新。
