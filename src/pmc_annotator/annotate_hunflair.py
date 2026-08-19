"""
HunFlair2 による NER + 正規化 (公式 flair API 準拠版)

3 つの動作モード:
  - linker_mode="full":  全アノテーションに linker (旧来の動作)
  - linker_mode="short": 短い表層形 (≤ short_threshold 文字) だけ linker (Phase 1 推奨)
  - linker_mode="none":  linker 完全スキップ (純粋 NER)
"""
from __future__ import annotations
from typing import Optional
from collections import defaultdict

from .schema import Document, Annotation, Passage


ENTITY_TYPE_MAP = {
    "Gene": "gene",
    "Disease": "disease",
    "Chemical": "chemical",
    "Species": "species",
    "CellLine": "cell_line",
    "Cell line": "cell_line",
}

INV_ENTITY_TYPE = {v: k for k, v in ENTITY_TYPE_MAP.items() if v != "cell_line"}

LINKER_NAMES = {
    "disease": "disease-linker",
    "chemical": "chemical-linker",
    "gene": "gene-linker",
    "species": "species-linker",
}

ID_PREFIX = {
    "gene": "NCBIGene:",
    "species": "NCBITaxon:",
}

# spaCy sentence splitter の文字数上限 (E088) を回避するための安全マージン
SPLITTER_MAX_CHARS = 900_000


def _split_long_text(text, splitter):
    """passage.text を splitter にかける。長文は境界で分割して E088 を回避し、
    各 sentence の (sent, global_start_position) を返す。
    global_start_position は passage.text 内のローカル位置 (既存のオフセット計算と互換)。"""
    results = []
    if len(text) <= SPLITTER_MAX_CHARS:
        for s in splitter.split(text):
            results.append((s, s.start_position))
        return results

    pos = 0
    n = len(text)
    while pos < n:
        end = min(pos + SPLITTER_MAX_CHARS, n)
        if end < n:
            cut = -1
            for sep in ('\n', '. ', ' '):
                idx = text.rfind(sep, pos, end)
                if idx > pos:
                    cut = idx + len(sep)
                    break
            if cut > pos:
                end = cut
        chunk = text[pos:end]
        for s in splitter.split(chunk):
            results.append((s, pos + s.start_position))
        pos = end
    return results

def _normalize_link_id(raw_id: str, entity_type: str) -> str:
    raw = raw_id.split("/")[0].strip()
    prefix = ID_PREFIX.get(entity_type, "")
    if prefix and not raw.startswith(prefix.rstrip(":")):
        raw = prefix + raw
    return raw


class HunFlairAnnotator:
    def __init__(self,
                 device: str = "cuda:0",
                 mini_batch_size: int = 32,
                 enable_linkers: Optional[bool] = None,
                 linker_mode: Optional[str] = None,
                 short_threshold: int = 3,
                 linker_types: Optional[list[str]] = None):
        self.device = device
        self.mini_batch_size = mini_batch_size
        self.short_threshold = short_threshold

        if linker_mode is None:
            if enable_linkers is None:
                linker_mode = "full"
            else:
                linker_mode = "full" if enable_linkers else "none"
        if linker_mode not in ("full", "short", "none"):
            raise ValueError(f"linker_mode must be 'full', 'short', or 'none', got {linker_mode!r}")
        self.linker_mode = linker_mode

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

        model_candidates = [
            "hunflair/hunflair2-ner",
            "hunflair2",
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

        if self.linker_mode != "none":
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

        all_sentences = []
        sentence_meta = []

        for p_idx, passage in enumerate(doc.passages):
            if not passage.text.strip():
                continue
            for s, gpos in _split_long_text(passage.text, self._splitter):
                all_sentences.append(s)
                sentence_meta.append((p_idx, gpos))

        if not all_sentences:
            return doc

        self._tagger.predict(all_sentences,
                             mini_batch_size=self.mini_batch_size,
                             verbose=False)

        if self.linker_mode == "full":
            for etype, linker in self._linkers.items():
                try:
                    linker.predict(all_sentences)
                except Exception as e:
                    print(f"[hunflair][warn] linker predict failed ({etype}): {e}")

        ann_counter = [0] * len(doc.passages)
        short_pending = []

        for sent, (p_idx, sent_offset_in_passage) in zip(all_sentences, sentence_meta):
            passage = doc.passages[p_idx]
            for span in sent.get_spans("ner"):
                ner_label = span.get_label("ner")
                tag = ner_label.value if ner_label else "Unknown"
                entity_type = ENTITY_TYPE_MAP.get(tag, tag.lower())

                local_offset = sent_offset_in_passage + span.start_position
                global_offset = passage.offset + local_offset

                identifiers = []
                if self.linker_mode == "full":
                    link_label = span.get_label("link")
                    if link_label and link_label.value and link_label.value != "O":
                        identifiers.append(_normalize_link_id(link_label.value, entity_type))

                ann_counter[p_idx] += 1
                ann = Annotation(
                    id=f"T{ann_counter[p_idx]}",
                    text=span.text,
                    offset=global_offset,
                    length=len(span.text),
                    entity_type=entity_type,
                    identifiers=identifiers,
                    score=float(ner_label.score) if ner_label else None,
                    source="hunflair2",
                )
                passage.annotations.append(ann)

                if (self.linker_mode == "short"
                        and len(span.text) <= self.short_threshold
                        and entity_type in LINKER_NAMES
                        and entity_type != "cell_line"):
                    short_pending.append((p_idx, len(passage.annotations) - 1,
                                          span.text, entity_type))

        if self.linker_mode == "short" and short_pending:
            self._process_short_pending(doc, short_pending)

        return doc

    def _process_short_pending(self, doc: Document,
                               short_pending: list) -> None:
        from flair.data import Sentence

        by_type: dict = defaultdict(lambda: defaultdict(list))
        for p_idx, a_idx, surface, etype in short_pending:
            by_type[etype][surface].append((p_idx, a_idx))

        for etype, surfaces_dict in by_type.items():
            linker = self._linkers.get(etype)
            if linker is None:
                continue
            surfaces = list(surfaces_dict.keys())

            sentences = []
            valid_surfaces = []
            for surf in surfaces:
                sent = Sentence(surf)
                if len(sent.tokens) == 0:
                    continue
                span = sent[0:len(sent.tokens)]
                span.add_label("ner", INV_ENTITY_TYPE[etype], score=1.0)
                sentences.append(sent)
                valid_surfaces.append(surf)

            if not sentences:
                continue

            try:
                linker.predict(sentences)
            except Exception as e:
                print(f"[hunflair][warn] short-linker predict failed ({etype}): {e}")
                continue

            for sent, surf in zip(sentences, valid_surfaces):
                spans = sent.get_spans("ner")
                if not spans:
                    continue
                link_label = spans[0].get_label("link")
                if not (link_label and link_label.value and link_label.value != "O"):
                    continue
                normalized = _normalize_link_id(link_label.value, etype)
                for p_idx, a_idx in surfaces_dict[surf]:
                    doc.passages[p_idx].annotations[a_idx].identifiers = [normalized]
