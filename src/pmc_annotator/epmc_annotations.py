"""Import accession annotations with explicit paper identity and local XML alignment."""
import argparse
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .preprocess import JATSParser
from .togoid_annotator import TogoIDAnnotator

API = 'https://www.ebi.ac.uk/europepmc/annotations_api/annotationsByArticleIds'
COLUMNS = ['doc_id', 'pmid', 'shard_id', 'passage_idx', 'passage_type',
           'ann_id', 'surface', 'entity_type', 'db', 'category', 'offset',
           'length', 'identifier', 'curie', 'confidence', 'source', 'raw_uri',
           'namespace', 'epmc_label', 'togoid_dataset', 'mapping_method',
           'label_namespace_consistent']


def documents(plan_path):
    plan = json.loads(Path(plan_path).read_text())
    parser = JATSParser()
    for shard in plan['shards']:
        for filename in shard['files']:
            doc = parser.parse(Path(filename))
            if doc is None:
                raise ValueError('Cannot parse ' + filename)
            if not re.fullmatch(r'PMC\d+', doc.id):
                raise ValueError('Expected PMCID, got ' + doc.id)
            yield shard['shard_id'], doc


def fetch(args):
    """Cache each request separately; errors never become successful empty results."""
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
    failures = []
    for _, doc in documents(args.plan):
        ids = ['PMC:' + doc.id[3:]]
        if args.include_med and doc.pmid:
            ids.append('MED:' + doc.pmid)
        for article_id in ids:
            path = out / (article_id.replace(':', '_') + '.json')
            if path.exists() and not args.refresh:
                cached = json.loads(path.read_text())
                if cached.get('doc_id') != doc.id:
                    raise ValueError('Cache paper mismatch: ' + str(path))
                continue
            try:
                response = session.get(API, params={
                    'articleIds': article_id, 'format': 'JSON-LD',
                    'type': 'Accession Numbers', 'provider': 'Europe PMC'}, timeout=60)
                response.raise_for_status()
                payload = response.json()
                annotations(payload)  # fail on unexpected response structures
                record = dict(doc_id=doc.id, requested_article_ids=[article_id],
                              retrieved_at=datetime.now(timezone.utc).isoformat(),
                              response=payload)
                temp = path.with_suffix('.tmp')
                temp.write_text(json.dumps(record, ensure_ascii=False))
                temp.replace(path)
                print(article_id, 'cached')
            except (requests.RequestException, ValueError) as exc:
                failures.append((article_id, str(exc)))
                print(article_id, 'FAILED:', exc)
            time.sleep(0.3)
    if failures:
        raise SystemExit(f'{len(failures)} requests failed; rerun to retry missing cache files')


def annotations(payload):
    """Accept JSON-LD arrays and documented article wrappers; reject error bodies."""
    if isinstance(payload, dict):
        if 'annotations' in payload:
            return annotations(payload['annotations'])
        if '@graph' in payload:
            return annotations(payload['@graph'])
        if 'target' in payload and 'body' in payload:
            return [payload]
        raise ValueError('Unexpected annotation response; inspect raw JSON')
    if isinstance(payload, list):
        result = []
        for item in payload:
            result.extend(annotations(item))
        return result
    raise ValueError('Expected JSON-LD annotation list')


def mapping_rules(path):
    data = yaml.safe_load(Path(path).read_text())
    return data, [(r['db'], re.compile(r['surface']), re.compile(r['uri']))
                  for r in data.get('rules', [])]


def identifiers_namespace(uri):
    """Return an identifiers.org prefix from either compact or path URI form.

    Examples: ``identifiers.org/geo:GSE1`` -> ``geo`` and
    ``identifiers.org/uniprot/P12345`` -> ``uniprot``.  This is deliberately
    local: normalisation does not make one network request per annotation.
    """
    match = re.match(r'^https?://(?:www\.)?identifiers\.org/(.+)$', uri,
                     flags=re.IGNORECASE)
    if not match:
        return None
    resource = match.group(1).split('?', 1)[0].split('#', 1)[0].lstrip('/')
    # Europe PMC commonly emits legacy EBI collection paths, for example
    # /ebi/bioproject:PRJNA..., rather than /bioproject:PRJNA....
    parts = resource.split('/', 1)
    if len(parts) == 2 and parts[0].lower() == 'ebi':
        resource = parts[1]
    prefix = re.split(r'[:/]', resource, maxsplit=1)[0].strip().lower()
    return prefix or None


def dynamic_dataset(exact, namespace, annotator):
    """Resolve a TogoID dataset key from a trusted identifiers.org namespace.

    The TogoID pattern catalogue is the source of the dataset keys, so this
    automatically follows additions to the extraction catalogue.  A namespace
    can still describe several datasets (notably GEO), in which case the
    accession pattern selects the unique subtype.
    """
    if not namespace:
        return None
    candidates = {
        hit.db for hit in annotator.annotate_text(exact)
        if hit.surface == exact
    }
    namespace_key = re.sub(r'[^a-z0-9]', '', namespace.lower())
    compatible = {
        db for db in candidates
        if re.sub(r'[^a-z0-9]', '', db.lower()).startswith(namespace_key)
    }
    return next(iter(compatible)) if len(compatible) == 1 else None


def resolve_dataset(exact, uri, config, rules, annotator):
    """Prefer dynamic identifiers.org + TogoID resolution; retain overrides."""
    namespace = identifiers_namespace(uri)
    aliases = config.get('namespace_aliases', {})
    if namespace in aliases:
        return aliases[namespace], namespace, 'namespace_alias'
    db = dynamic_dataset(exact, namespace, annotator)
    if db:
        return db, namespace, 'namespace+togoid_pattern'
    # Some providers use their own URI rather than identifiers.org.  The small
    # override file also covers namespace aliases that cannot be inferred from
    # a dataset key alone.
    dbs = {db for db, pattern, urx in rules
           if pattern.fullmatch(exact) and urx.search(uri)}
    return (next(iter(dbs)) if len(dbs) == 1 else None), namespace, \
        ('uri_override' if len(dbs) == 1 else None)


def label_consistent(label, namespace, config):
    """Return True/False when the label is known, otherwise None (not a claim)."""
    if not label:
        return None
    expected = config.get('label_namespaces', {}).get(str(label).lower())
    if not expected:
        return None
    return namespace in expected


def locate(doc, exact, selector, section=None):
    all_hits, section_hits = [], []
    for idx, passage in enumerate(doc.passages):
        for m in re.finditer(re.escape(exact), passage.text):
            hit = (idx, passage, m.start())
            all_hits.append(hit)
            if section and passage.infon.get('section_type', '').lower() == section.lower():
                section_hits.append(hit)
    # Europe PMC can use a fine-grained heading while JATSParser stores the
    # parent section as "body".  Prefer a matching section, but do not discard
    # an otherwise unique quoted match merely because the vocabularies differ.
    hits = section_hits or all_hits
    if len(hits) <= 1:
        return hits
    prefix, suffix = selector.get('prefix', ''), selector.get('suffix', '')
    if not (prefix or suffix):
        return []
    return [(idx, p, pos) for idx, p, pos in hits
            if (not prefix or normalise_context(p.text[:pos]).endswith(normalise_context(prefix)))
            and (not suffix or normalise_context(p.text[pos + len(exact):]).startswith(normalise_context(suffix)))]


def normalise_context(text):
    """Compare quoted contexts despite XML's Unicode whitespace differences."""
    text = unicodedata.normalize('NFKC', text).replace('\ufffd', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def local_candidates(doc, exact, radius=80):
    """Compact evidence for human review when alignment is not unique."""
    candidates = []
    for idx, passage in enumerate(doc.passages):
        for match in re.finditer(re.escape(exact), passage.text):
            start = max(0, match.start() - radius)
            end = min(len(passage.text), match.end() + radius)
            candidates.append({
                'passage_idx': idx,
                'passage_type': passage.infon.get('section_type', 'body'),
                'offset': passage.offset + match.start(),
                'context': passage.text[start:end],
            })
    return candidates


def normalize(args):
    docs = {doc.id: (sid, doc) for sid, doc in documents(args.plan)}
    config, rules = mapping_rules(args.mapping)
    annotator = TogoIDAnnotator(getattr(
        args, 'patterns', Path(__file__).parent / 'data/togoid_extract_patterns.yaml'))
    rows, external, rejected, conflicts, coverage = [], [], [], [], []
    for path in sorted(Path(args.raw).glob('*.json')):
        record = json.loads(path.read_text())
        doc_id = record['doc_id']
        if doc_id not in docs:
            continue
        sid, doc = docs[doc_id]
        anns = annotations(record['response'])
        coverage.append({'doc_id': doc_id, 'request': record['requested_article_ids'][0],
                         'n_annotations': len(anns)})
        for ann in anns:
            if ann.get('type') != 'Accession Numbers':
                continue  # never convert entity-linked gene names to accession citations
            target = ann.get('target', {})
            selector = target.get('selector', {})
            exact = selector.get('exact', '')
            uri = ann.get('body', '')
            if isinstance(uri, dict):
                uri = uri.get('id', '')
            if not isinstance(uri, str) or not isinstance(exact, str) or not exact:
                rejected.append(dict(doc_id=doc_id, reason='missing_text_or_uri', annotation=ann))
                continue
            label = ann.get('memberOf', '')
            db, namespace, method = resolve_dataset(exact, uri, config, rules, annotator)
            consistent = label_consistent(label, namespace, config)
            if consistent is False:
                conflicts.append(dict(doc_id=doc_id, epmc_label=label,
                    idorg_namespace=namespace, annotation=ann))
            hits = locate(doc, exact, selector, target.get('isPartOf'))
            is_external = namespace in set(config.get('non_accession_namespaces', []))
            reason = ('unmapped_or_ambiguous_namespace' if not db and not is_external else
                      'missing_or_ambiguous_local_span' if not is_external and len(hits) != 1 else '')
            if reason:
                record = dict(doc_id=doc_id, reason=reason, annotation=ann)
                if reason == 'missing_or_ambiguous_local_span':
                    record['local_candidates'] = local_candidates(doc, exact)
                rejected.append(record)
                continue
            if hits:
                idx, passage, pos = hits[0]
                passage_type = passage.infon.get('section_type', 'body')
                offset = passage.offset + pos
            else:
                # External identifiers are intentionally not used in context
                # windows or accession verification, so an XML offset is not
                # required to preserve Europe PMC's DOI annotation.
                idx, passage_type, offset = None, None, None
            record = dict(doc_id=doc_id, pmid=doc.pmid, shard_id=sid,
                passage_idx=idx, passage_type=passage_type,
                ann_id=ann.get('id', ''), surface=exact, entity_type='accession', db=db,
                category='imported', offset=offset, length=len(exact),
                identifier=f'{db or namespace}:{exact}', curie=uri, confidence='imported',
                source='europepmc', raw_uri=uri, namespace=namespace, epmc_label=label,
                togoid_dataset=db, mapping_method=method or 'excluded_non_accession',
                label_namespace_consistent=consistent)
            if is_external:
                record['entity_type'] = 'external_identifier'
                record['category'] = 'external_identifier'
                record['db'] = namespace
                external.append(record)
            else:
                rows.append(record)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=COLUMNS).drop_duplicates(
        ['doc_id', 'db', 'surface', 'offset']).to_parquet(out / 'shard_epmc.parquet', index=False)
    pd.DataFrame(external, columns=COLUMNS).drop_duplicates(
        ['doc_id', 'db', 'surface', 'offset']).to_parquet(
            out / 'external_identifiers.parquet', index=False)
    (out / 'rejected.json').write_text(json.dumps(rejected, indent=2))
    (out / 'mapping_conflicts.json').write_text(json.dumps(conflicts, indent=2))
    (out / 'coverage.json').write_text(json.dumps(coverage, indent=2))
    missing = sorted(set(docs) - {r['doc_id'] for r in coverage})
    (out / 'missing_papers.json').write_text(json.dumps(missing, indent=2))
    print(f'{len(rows)} aligned accessions; {len(external)} external identifiers; '
          f'{len(rejected)} rejected; {len(conflicts)} label/namespace conflicts; '
          f'{len(missing)} papers without cache')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('fetch')
    p.add_argument('--plan', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--include-med', action='store_true', help='Also query MED abstract annotations')
    p.add_argument('--refresh', action='store_true')
    p.set_defaults(func=fetch)
    p = sub.add_parser('normalize')
    p.add_argument('--plan', required=True)
    p.add_argument('--raw', required=True)
    p.add_argument('--mapping', default=str(Path(__file__).parent / 'data/epmc_mapping.yaml'))
    p.add_argument('--patterns', default=str(Path(__file__).parent / 'data/togoid_extract_patterns.yaml'),
                   help='TogoID extraction catalogue used to derive dataset keys')
    p.add_argument('--output', required=True)
    p.set_defaults(func=normalize)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
