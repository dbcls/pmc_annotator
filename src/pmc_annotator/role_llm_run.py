"""
role_llm_run.py — 役割判定 部品(b2): OSS(OpenAI互換)/Azure で LLM 呼び出し

入力: llm_payloads.jsonl(中立ペイロード) + role_prelim.tsv(rule_final ロール+class)
各 doc を OpenAI互換 chat/completions に投げ structured JSON {results:[{id,role,evidence_span,confidence}]}
を得る。複数 --base-url をラウンドロビン(データ並列)。rule_final と合流し usage_roles.tsv 出力。

Qwen3系メモ: 分類では非思考モード(--thinking を付けない=既定)で <think> を抑止。
             temperature=0 はループするので既定 0.2 + top_p/top_k。
"""
from __future__ import annotations
import argparse, csv, json, os, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request, urllib.error

ROLES = ["generated", "used", "mentioned"]

SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "role": {"type": "string", "enum": ROLES},
                    "evidence_span": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["id", "role", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def http_post(url, headers, body, timeout):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]   # vLLM の実エラー文を回収
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None


def build_request(payload, model, json_mode, backend, temperature, top_p, top_k, thinking):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": payload["system_text"]},
                     {"role": "user", "content": payload["user_text"]}],
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": payload.get("max_tokens", 1024),
    }
    if backend == "oss":
        if top_k is not None:
            body["top_k"] = top_k                              # vLLM 拡張
        if not thinking:                                       # Qwen3系: 思考トークンを切る
            body["chat_template_kwargs"] = {"enable_thinking": False}
    if json_mode == "schema":       # OpenAI標準(Azure/新しめvLLM/Ollama)
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "roles", "schema": SCHEMA, "strict": True}}
    elif json_mode == "guided":     # vLLM 独自(extra body)
        body["guided_json"] = SCHEMA
    elif json_mode == "object":     # 最低限(schema非対応時)
        body["response_format"] = {"type": "json_object"}
    return body


def endpoint_and_headers(backend, base_url, model, azure_api_version):
    if backend == "oss":
        return base_url.rstrip("/") + "/v1/chat/completions", {"Content-Type": "application/json"}
    if backend == "azure":
        ep = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
        url = f"{ep}/openai/deployments/{model}/chat/completions?api-version={azure_api_version}"
        return url, {"Content-Type": "application/json", "api-key": os.environ["AZURE_OPENAI_API_KEY"]}
    raise SystemExit(f"unknown backend: {backend}")


def parse_results(resp):
    obj = json.loads(resp["choices"][0]["message"]["content"])
    out = {}
    for r in obj.get("results", []):
        if "id" in r and "role" in r:
            out[str(r["id"])] = {"role": r["role"],
                                 "evidence_span": r.get("evidence_span", ""),
                                 "confidence": r.get("confidence", "")}
    return out


def call_doc(payload, urls, backend, model, json_mode, gen, azure_ver, timeout, retries, start):
    body = build_request(payload, model, json_mode, backend,
                         gen["temperature"], gen["top_p"], gen["top_k"], gen["thinking"])
    last = None
    for attempt in range(retries + 1):
        base = urls[(start + attempt) % len(urls)] if urls else None
        url, headers = endpoint_and_headers(backend, base, model, azure_ver)
        try:
            return parse_results(http_post(url, headers, body, timeout)), None
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(min(2 ** attempt, 8))
    return {}, last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payloads", required=True)
    ap.add_argument("--prelim", required=True)
    ap.add_argument("--out", required=True, help="usage_roles.tsv")
    ap.add_argument("--raw", default=None, help="生LLM出力 jsonl(任意・監査用)")
    ap.add_argument("--backend", choices=["oss", "azure"], default="oss")
    ap.add_argument("--base-url", action="append", default=[],
                    help="OSS: OpenAI互換ベースURL。複数指定でラウンドロビン(データ並列)")
    ap.add_argument("--model", required=True, help="OSS=served-model-name / Azure=deployment名")
    ap.add_argument("--json-mode", choices=["schema", "guided", "object"], default="schema")
    ap.add_argument("--azure-api-version", default="2024-10-21")
    ap.add_argument("--temperature", type=float, default=0.2)  # Qwen3系はtemp=0でループ→非0
    ap.add_argument("--top-p", type=float, default=0.9)
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--thinking", action="store_true", help="思考モード有効化(既定=無効)")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--smoke", action="store_true", help="先頭1 docだけ投げ生応答を表示して終了")
    args = ap.parse_args()

    if args.backend == "oss" and not args.base_url:
        args.base_url = ["http://localhost:8000"]

    gen = {"temperature": args.temperature, "top_p": args.top_p,
           "top_k": args.top_k, "thinking": args.thinking}

    payloads = []
    with open(args.payloads) as f:
        for line in f:
            if line.strip():
                payloads.append(json.loads(line))
    if args.limit:
        payloads = payloads[:args.limit]
    if not payloads:
        sys.exit("[info] ペイロードが空です")

    if args.smoke:
        p = payloads[0]
        url, headers = endpoint_and_headers(args.backend, args.base_url[0] if args.base_url else None,
                                            args.model, args.azure_api_version)
        print(f"[smoke] POST {url}  doc={p['doc_id']}  entities={p['n_entities']}  "
              f"json_mode={args.json_mode}  thinking={args.thinking}", file=sys.stderr)
        resp = http_post(url, headers,
                         build_request(p, args.model, args.json_mode, args.backend,
                                       gen["temperature"], gen["top_p"], gen["top_k"], gen["thinking"]),
                         args.timeout)
        print(resp["choices"][0]["message"]["content"])
        return

    results, errors = {}, {}
    raw_fo = open(args.raw, "w") if args.raw else None
    lock = threading.Lock()

    def work(i_p):
        i, p = i_p
        res, err = call_doc(p, args.base_url, args.backend, args.model, args.json_mode, gen,
                            args.azure_api_version, args.timeout, args.retries, i)
        return p["doc_id"], res, err

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [ex.submit(work, (i, p)) for i, p in enumerate(payloads)]
        done = 0
        for fut in as_completed(futs):
            doc_id, res, err = fut.result()
            with lock:
                results.setdefault(doc_id, {}).update(res)   # 複数チャンクを累積(上書き防止)
                if err:
                    errors[doc_id] = err
                if raw_fo:
                    raw_fo.write(json.dumps({"doc_id": doc_id, "results": res, "error": err},
                                            ensure_ascii=False) + "\n")
                done += 1
                if done % 100 == 0:
                    print(f"  ..{done}/{len(payloads)} docs (errors={len(errors)})", file=sys.stderr)
    if raw_fo:
        raw_fo.close()

    # 合流: role_prelim 全行 -> usage_roles.tsv
    cnt = {"rule": 0, "llm": 0, "llm_missing": 0, "llm_error": 0, "skip": 0}
    with open(args.prelim, newline="") as fi, open(args.out, "w", newline="") as fo:
        w = csv.writer(fo, delimiter="\t")
        w.writerow(["doc_id", "dataset", "id", "class", "role", "role_source",
                    "role_confidence", "evidence_span", "model", "note"])
        for r in csv.DictReader(fi, delimiter="\t"):
            doc_id, dataset, ent_id, cls = r["doc_id"], r["dataset"], r["id"], r.get("class", "")
            if r.get("to_llm") != "1":
                role = r.get("role", "")
                if not role:
                    cnt["skip"] += 1
                    continue
                w.writerow([doc_id, dataset, ent_id, cls, role, "rule", "", "", "", r.get("note", "")])
                cnt["rule"] += 1
                continue
            got = results.get(doc_id, {}).get(ent_id)
            if got is not None:
                w.writerow([doc_id, dataset, ent_id, cls, got["role"], "llm",
                            got.get("confidence", ""), got.get("evidence_span", ""),
                            args.model, r.get("prior", "")])
                cnt["llm"] += 1
            elif doc_id in errors:
                w.writerow([doc_id, dataset, ent_id, cls, "error", "llm", "", "", args.model,
                            errors[doc_id][:80]])
                cnt["llm_error"] += 1
            else:
                w.writerow([doc_id, dataset, ent_id, cls, "missing", "llm", "", "", args.model,
                            "id_not_in_output"])
                cnt["llm_missing"] += 1

    print("[summary] " + "  ".join(f"{k}={v}" for k, v in cnt.items()) +
          f"  doc_errors={len(errors)}", file=sys.stderr)


if __name__ == "__main__":
    main()
