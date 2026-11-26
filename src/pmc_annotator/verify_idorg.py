#!/usr/bin/env python3
"""Broad-coverage existence verification via Identifiers.org's own resolver.

T2 (SPARQL against specific RDF Portal graphs) and T3 (NCBI E-utilities)
each cover a hand-picked set of databases. This layer asks Identifiers.org
itself to resolve a CURIE and follow the redirect -- since identifiers.org
already knows every registered namespace's local-ID conventions, it covers
any dataset key reconcile_namespaces.py could map to a registry prefix,
without us needing per-database URL templates.

A namespace either expects "<prefix>:<local_id>" (e.g. "uniprot:P04637")
or, when its local IDs already embed the namespace (namespaceEmbeddedInLui,
e.g. ChEBI's "CHEBI:15377", GO's "GO:0008150"), just "<local_id>" alone --
prefixing it again 404s. Confirmed empirically: identifiers.org/chebi:15377
-> 404, identifiers.org/CHEBI:15377 -> 200, redirected to EBI.

We never assert absence: a resolution failure only means identifiers.org
either didn't recognise the CURIE as well-formed or had nothing to redirect
to for it -- not that the ID doesn't exist at the source. A confirmed
result means identifiers.org routed the request off its own domain to a
live provider page.

  python3 verify_idorg.py route --in verified.t3.tsv       # coverage preview, no requests
  python3 verify_idorg.py run   --in verified.t3.tsv --out verified.idorg.tsv --workers 8
"""
import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

IDORG = 'https://identifiers.org/{path}'
CACHE = 't_idorg_cache.sqlite'
_REPO_ROOT = Path(__file__).parent.parent.parent
BULK_REGISTRY = _REPO_ROOT / 'data' / 'identifiers_registry' / 'idorg_resolver_dataset.json'
RECONCILIATION = _REPO_ROOT / 'outputs' / 'namespace_reconciliation.tsv'


def load_bulk_registry(path):
    data = json.loads(Path(path).read_text())
    namespaces = (data.get('payload') or {}).get('namespaces') or []
    return {n['prefix'].lower(): n for n in namespaces if n.get('prefix')}


def load_dataset_prefixes(path):
    """dataset_key -> registry_prefix, from reconcile_namespaces.py's TSV report."""
    import csv
    mapping = {}
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f, delimiter='\t'):
            if row.get('registry_prefix'):
                mapping[row['dataset_key']] = row['registry_prefix']
    return mapping


def resolution_path(prefix, local_id, embedded):
    return local_id if embedded else f'{prefix}:{local_id}'


def session_with_retries(pool_size=10):
    session = requests.Session()
    session.mount('https://', HTTPAdapter(
        max_retries=Retry(total=3, backoff_factor=1,
                          status_forcelist=[429, 500, 502, 503, 504]),
        pool_connections=pool_size, pool_maxsize=pool_size))
    return session


def resolve_one(session, path, timeout=30):
    """GET identifiers.org/<path>. 'confirmed' only if it redirected off
    identifiers.org's own domain to a live provider page; else 'pending'."""
    try:
        resp = session.get(IDORG.format(path=urllib.parse.quote(path, safe=':/')),
                            timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        return 'pending'
    if resp.status_code >= 400:
        return 'pending'
    if urllib.parse.urlparse(resp.url).netloc.endswith('identifiers.org'):
        return 'pending'  # never left identifiers.org (e.g. an unresolved landing page)
    return 'confirmed'


def _resolve_task(session, dataset, local_id, prefix, embedded, sleep):
    """One lookup; run concurrently across threads against the shared session."""
    status = resolve_one(session, resolution_path(prefix, local_id, embedded))
    time.sleep(sleep)
    return dataset, local_id, status


# ---- cache (mirrors verify_t3.py's sqlite pattern) ----
def open_cache():
    conn = sqlite3.connect(CACHE)
    conn.execute('CREATE TABLE IF NOT EXISTS idorg(dataset TEXT, id TEXT, status TEXT, '
                 'PRIMARY KEY(dataset, id))')
    return conn


def cache_load(conn, dataset):
    return {r[0]: r[1] for r in conn.execute(
        'SELECT id, status FROM idorg WHERE dataset=?', (dataset,))}


def cache_save(conn, dataset, results):
    conn.executemany('INSERT OR REPLACE INTO idorg VALUES(?,?,?)',
                     [(dataset, i, s) for i, s in results.items()])
    conn.commit()


# ---- verified.tsv read/write (same 4-column shape as verify_t2/verify_t3) ----
def read_verified(path):
    header, rows = None, []
    for line in open(path, encoding='utf-8'):
        line = line.rstrip('\n')
        if not line:
            continue
        if header is None and line.split('\t')[0] == 'dataset':
            header = line
            continue
        rows.append(line.split('\t'))
    return header, rows


def cmd_route(args):
    _, rows = read_verified(args.__dict__['in'])
    prefixes = load_dataset_prefixes(args.reconciliation)
    registry = load_bulk_registry(args.registry)
    pending = Counter(r[0] for r in rows if len(r) > 2 and r[2] == 'pending')
    covered = {ds: n for ds, n in pending.items()
               if prefixes.get(ds, '').lower() in registry}
    print(f'pending datasets = {len(pending)}  pending rows = {sum(pending.values()):,}')
    print(f'covered by a resolvable registry prefix = {len(covered)} datasets, '
          f'{sum(covered.values()):,} rows\n')
    for ds, n in pending.most_common():
        prefix = prefixes.get(ds, '')
        covered_mark = '*' if ds in covered else ' '
        embedded = registry.get(prefix.lower(), {}).get('namespaceEmbeddedInLui') if prefix else None
        print(f'  {covered_mark} {ds:22s} {n:>7,d}'
              + (f'  -> {prefix} (embedded_lui={embedded})' if prefix else '  (no registry prefix)'))


def cmd_run(args):
    header, rows = read_verified(args.__dict__['in'])
    prefixes = load_dataset_prefixes(args.reconciliation)
    registry = load_bulk_registry(args.registry)

    pending = defaultdict(set)
    for r in rows:
        if len(r) > 2 and r[2] == 'pending':
            pending[r[0]].add(r[1])

    conn = open_cache()
    session = session_with_retries(pool_size=max(args.workers, 10))
    result = {}  # (dataset, id) -> status
    caches = {}  # dataset -> {id: status}, loaded once, updated as work completes
    tasks = []   # (dataset, local_id, prefix, embedded), only for cache misses
    skipped_datasets = []
    for dataset, ids in pending.items():
        prefix = prefixes.get(dataset)
        entry = registry.get(prefix.lower()) if prefix else None
        if not entry:
            skipped_datasets.append(dataset)
            continue  # no registry prefix for this dataset key -- leave pending
        embedded = bool(entry.get('namespaceEmbeddedInLui'))
        cache = cache_load(conn, dataset)
        caches[dataset] = cache
        for local_id in sorted(ids):
            if local_id in cache:
                result[(dataset, local_id)] = cache[local_id]
            else:
                tasks.append((dataset, local_id, prefix, embedded))

    if skipped_datasets:
        print(f'no registry prefix, left pending: {sorted(set(skipped_datasets))}')
    print(f'{len(tasks)} lookups to run ({len(result)} already cached), '
          f'{args.workers} workers', flush=True)

    new_by_dataset = defaultdict(dict)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_resolve_task, session, ds, local_id, prefix, embedded, args.sleep)
                   for ds, local_id, prefix, embedded in tasks]
        done = 0
        for future in as_completed(futures):
            dataset, local_id, status = future.result()
            new_by_dataset[dataset][local_id] = status
            result[(dataset, local_id)] = status
            done += 1
            print(f'[{done}/{len(tasks)}] {dataset}\t{local_id}\t{status}', flush=True)

    for dataset, updates in new_by_dataset.items():
        cache_save(conn, dataset, updates)

    for dataset, ids in pending.items():
        if dataset not in caches:
            continue
        n_confirmed = sum(1 for i in ids if result.get((dataset, i)) == 'confirmed')
        print(f'{dataset:22s} {len(ids):>6,d} pending -> confirmed={n_confirmed:,} '
              f'via {prefixes.get(dataset)}')

    with open(args.out, 'w', encoding='utf-8') as w:
        w.write((header or 'dataset\tid\tstatus\toracle') + '\n')
        n_newly_confirmed = 0
        for r in rows:
            while len(r) < 4:
                r.append('-')
            dataset, local_id, status, oracle = r[0], r[1], r[2], r[3]
            if status == 'pending' and (dataset, local_id) in result:
                new_status = result[(dataset, local_id)]
                if new_status == 'confirmed':
                    status, oracle = new_status, 'idorg_resolution'
                    n_newly_confirmed += 1
            w.write(f'{dataset}\t{local_id}\t{status}\t{oracle}\n')
    print(f'\nconfirmed via identifiers.org resolution = {n_newly_confirmed:,}  -> {args.out}')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name, fn in (('route', cmd_route), ('run', cmd_run)):
        p = sub.add_parser(name)
        p.add_argument('--in', dest='in', required=True, help='verified.tsv (from verify_t2/verify_t3)')
        p.add_argument('--registry', default=str(BULK_REGISTRY),
                       help='Offline Identifiers.org bulk export')
        p.add_argument('--reconciliation', default=str(RECONCILIATION),
                       help='namespace_reconciliation.tsv (dataset_key -> registry_prefix)')
        if name == 'run':
            p.add_argument('--out', default='verified.idorg.tsv')
            p.add_argument('--workers', type=int, default=8,
                           help='Concurrent request threads (single shared host -- keep modest)')
            p.add_argument('--sleep', type=float, default=0.2,
                           help='Delay each worker takes after its own request (applies per thread)')
        p.set_defaults(func=fn)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
