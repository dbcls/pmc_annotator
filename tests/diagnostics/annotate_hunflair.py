"""
HunFlair2 による NER + 正規化 (公式 flair API 準拠版)

確定した API 仕様 (flair 0.14+ 公式ドキュメントより):
- NER:  tagger = Classifier.load("hunflair2"); tagger.predict(sentences)
        → span.get_label("ner").value で "Gene"/"Disease"/... が取れる
- NEN:  linker = EntityMentionLinker.load("<type>-linker"); linker.predict(sentences)
        → span.get_label("link").value で正規化IDが取れる
        → ラベルタイプは "link" で固定

正規化IDのフォーマット (linker依存):
- disease-linker:  "MESH:Dxxxxxx"        (CTD Diseases / MeSH)
- chemical-linker: "MESH:Dxxxxxx"        (CTD Chemicals / MeSH)
- gene-linker:     "108684022"           (NCBI Gene, プレフィックス無し, **Humanのみ**)
- species-linker:  "10090"               (NCBI Taxonomy, プレフィックス無し)
- cell_line:       linker無し → NERのみ

落とし穴:
- 略語解決の精度向上には pyab3p が必要 (デフォルト未同梱・OS依存)。
  入っていなくても動くが、"FXS" のような略語の正規化精度が落ちる。
- gene-linker は Human 限定。非ヒト遺伝子は別途対応が要る。
- linker の predict は重い (初回はオントロジー前処理で時間がかかる)。
"""
from __future__ import annotations
from typing import Optional

from .schema import Document, Annotation, Passage


# HunFlair2 の NER タグ ("ner" ラベル) -> 内部 entity_type
ENTITY_TYPE_MAP = {
    "Gene": "gene",
    "Disease": "disease",
    "Chemical": "chemical",
    "Species": "species",
    "CellLine": "cell_line",
    "Cell line": "cell_line",
}

# entity_type -> linker 名 (cell_line は linker 無し)
LINKER_NAMES = {
    "disease": "disease-linker",
    "chemical": "chemical-linker",
    "gene": "gene-linker",
    "species": "species-linker",
}

# linker が返す生IDに付けるプレフィックス (既に付いているものは触らない)
# gene/species はプレフィックス無しの数値IDなので補う
ID_PREFIX = {
    "gene": "NCBIGene:",
    "species": "NCBITaxon:",
    # disease/chemical は "MESH:..." が既に付いているので何もしない
}


class HunFlairAnnotator:
    """HunFlair2 によるアノテーター。__call__(Document) -> Document。

    使い方:
        annotator = HunFlairAnnotator(device="cuda:0", enable_linkers=True)
        annotator.load()
        doc = annotator(doc)
    """

    def __init__(self,
                 device: str = "cuda:0",
                 mini_batch_size: int = 32,
                 enable_linkers: bool = True,
                 linker_types: Optional[list[str]] = None):
        self.device = device
        self.mini_batch_size = mini_batch_size
        self.enable_linkers = enable_linkers
        self.linker_types = linker_types or ["disease", "chemical", "gene", "species"]
        self._tagger = None
        self._splitter = None
        self._linkers: dict = {}

    def load(self):
        if self._tagger is not None:
            return
        import flair
        import torch
        from flair.nn import Classifier
        from flair.splitter import SciSpacySentenceSplitter

        flair.device = torch.device(self.device)

        # HunFlair2 tagger のロード:
        # flair 0.14+ では Classifier.load("hunflair2") が断続的に
        # ValueError "key 'hunflair2' was neither found on the ModelHub..."
        # で失敗することがある (GitHub flairNLP/flair#3547)。
        # 直接 HuggingFace パスを指定する方が確実なので、こちらを優先し、
        # ダメなら旧エイリアスにフォールバックする。
        model_candidates = [
            "hunflair/hunflair2-ner",  # 直接 HF パス (推奨, 最も安定)
            "hunflair2",               # エイリアス (互換性のため)
        ]
        last_err = None
        for name in model_candidates:
            try:
                self._tagger = Classifier.load(name)
                print(f"[hunflair] loaded tagger: {name}")
                break
            except Exception as e:
                last_err = e
                print(f"[hunflair][warn] failed to load '{name}': {e}")
        if self._tagger is None:
            raise RuntimeError(
                f"Could not load HunFlair2 tagger. Last error: {last_err}\n"
                f"Hint: try `rm -rf ~/.flair/models/hunflair2-ner` and re-run."
            )

        self._splitter = SciSpacySentenceSplitter()

        if self.enable_linkers:
            from flair.models import EntityMentionLinker
            for etype in self.linker_types:
                linker_name = LINKER_NAMES.get(etype)
                if not linker_name:
                    continue
                try:
                    self._linkers[etype] = EntityMentionLinker.load(linker_name)
                    print(f"[hunflair] loaded linker: {linker_name}")
                except Exception as e:
                    print(f"[hunflair][warn] linker '{linker_name}' load failed: {e}")

    def __call__(self, doc: Document) -> Document:
        if self._tagger is None:
            self.load()

        # passage ごとに文分割し、Sentence と (passage_idx, passage先頭からのオフセット) を保持
        all_sentences = []
        sentence_meta = []  # (passage_idx, sentence_start_char_in_passage)

        for p_idx, passage in enumerate(doc.passages):
            if not passage.text.strip():
                continue
            sents = self._splitter.split(passage.text)
            for s in sents:
                all_sentences.append(s)
                # Sentence.start_position = 元テキスト(=passage.text)内の開始文字位置
                sentence_meta.append((p_idx, s.start_position))

        if not all_sentences:
            return doc

        # --- NER ---
        self._tagger.predict(all_sentences, mini_batch_size=self.mini_batch_size, verbose=False)

        # --- NEN (linker) ---
        for etype, linker in self._linkers.items():
            try:
                linker.predict(all_sentences)
            except Exception as e:
                print(f"[hunflair][warn] linker predict failed ({etype}): {e}")

        # --- 結果を Annotation 化 ---
        ann_counter = [0] * len(doc.passages)
        for sent, (p_idx, sent_offset_in_passage) in zip(all_sentences, sentence_meta):
            passage = doc.passages[p_idx]
            for span in sent.get_spans("ner"):
                ner_label = span.get_label("ner")
                tag = ner_label.value if ner_label else "Unknown"
                entity_type = ENTITY_TYPE_MAP.get(tag, tag.lower())

                # span.start_position は Sentence 内のオフセット
                local_offset = sent_offset_in_passage + span.start_position
                global_offset = passage.offset + local_offset

                # --- 正規化ID取得: get_label("link") ---
                identifiers = []
                link_label = span.get_label("link")
                if link_label and link_label.value and link_label.value != "O":
                    raw_id = link_label.value
                    # "108684022/name=FRAXA" のように name= が付くケースを分離
                    raw_id = raw_id.split("/")[0].strip()
                    prefix = ID_PREFIX.get(entity_type, "")
                    if prefix and not raw_id.startswith(prefix.rstrip(":")):
                        raw_id = prefix + raw_id
                    identifiers.append(raw_id)

                ann_counter[p_idx] += 1
                passage.annotations.append(Annotation(
                    id=f"T{ann_counter[p_idx]}",
                    text=span.text,
                    offset=global_offset,
                    length=len(span.text),
                    entity_type=entity_type,
                    identifiers=identifiers,
                    score=float(ner_label.score) if ner_label else None,
                    source="hunflair2",
                ))

        return doc
