"""
clean_noise_vocab.py
noise_vocab.json から「本物の正当なID」を除外し、真の誤検出語彙だけを残す。
本物IDを否定辞書に残すと正しい抽出選択肢まで除去してしまうため。
"""
import json
import re
from pathlib import Path

vocab = json.load(open("noise_vocab.json"))

# 「本物の正当なID」と判定するパターン (これらは否定辞書から除外)
LEGIT_ID_PATTERNS = [
    r'^ENS[A-Z]*[GTPE]\d{6,}$',        # Ensembl 各種 (ENST00000035241, ENSMUST..., ENSBTAT...)
    r'^ENSFM\d+$',                      # Ensembl family
    r'^NM_\d+',                         # RefSeq mRNA
    r'^N[PRCGTW]_\d+',                  # RefSeq 各種 (NP_/NR_/NC_/NG_/NT_/NW_)
    r'^[XY][MPR]_\d+',                  # RefSeq predicted (XM_/XP_/XR_/YP_)
    r'^FBtr\d+$',                       # FlyBase transcript
    r'^AGAP\d+$',                       # Anopheles gene (本物)
    r'^AAEL\d+$',                       # Aedes gene (本物)
    r'^ENSANGT\d+$|^ENSAPMT\d+$',       # Ensembl 異種 transcript
]
_legit = [re.compile(p) for p in LEGIT_ID_PATTERNS]

def is_legit_id(token):
    return any(p.match(token) for p in _legit)

kept = [t for t in vocab if not is_legit_id(t)]
removed = [t for t in vocab if is_legit_id(t)]

print(f"元の語彙: {len(vocab)}")
print(f"本物IDとして除外: {len(removed)}")
print(f"否定辞書に残す真の誤検出語彙: {len(kept)}")
print()
print("=== 除外した本物ID (サンプル20) ===")
for t in removed[:20]:
    print(f"  {t}")

with open("noise_vocab_clean.json", "w") as f:
    json.dump(sorted(kept), f, ensure_ascii=False, indent=0)
print("\n→ noise_vocab_clean.json に保存")
