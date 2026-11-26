"""Audit pipeline dataset keys against the Identifiers.org namespace registry."""
import argparse
import csv
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

REGISTRY = 'https://registry.api.identifiers.org/restApi/namespaces/search/findByPrefix'
FIELDS = ['dataset_key', 'pattern_layer', 'category', 'extraction_pattern',
          'registry_prefix', 'mapping_type', 'mapping_source', 'registry_status',
          'registry_name', 'registry_pattern', 'registry_deprecated', 'current_uri',
          'review_note']


def load_bulk_registry(path):
    """Index the offline Identifiers.org export (payload.namespaces) by prefix.

    The live REST API is unreachable from this environment, so the bulk
    export at data/identifiers_registry/idorg_resolver_dataset.json is the
    primary lookup source; the live API remains a fallback for prefixes it
    doesn't cover.
    """
    if not path or not Path(path).exists():
        return {}
    data = json.loads(Path(path).read_text())
    namespaces = (data.get('payload') or {}).get('namespaces') or []
    index = {}
    for namespace in namespaces:
        prefix = (namespace.get('prefix') or '').lower()
        if prefix and prefix not in index:
            index[prefix] = namespace
    return index


def prefix_from_uri(uri):
    if not uri:
        return None
    parsed = urlparse(uri)
    if parsed.netloc.lower() not in {'identifiers.org', 'www.identifiers.org'}:
        return None
    value = parsed.path.strip('/')
    if value.lower().startswith('ebi/'):
        value = value[4:]
    return re.split(r'[:/]', value, maxsplit=1)[0].lower() or None


def registry_record(prefix, cache, refresh, bulk_index):
    bulk_hit = bulk_index.get(prefix)
    if bulk_hit is not None:
        return bulk_hit
    path = cache / f'{prefix}.json'
    if path.exists() and not refresh:
        cached = json.loads(path.read_text())
        # Network failures are diagnostics, never durable registry data.
        if '_error' not in cached:
            return cached
    try:
        response = requests.get(REGISTRY, params={'prefix': prefix}, timeout=30)
        if response.status_code == 404:
            result = {'_missing': True}
        else:
            response.raise_for_status()
            result = response.json()
    except requests.RequestException as error:
        result = {'_error': str(error)}
    path.write_text(json.dumps(result, indent=2))
    return result


def registry_fields(record):
    if record.get('_missing'):
        return 'not_found', '', '', ''
    if record.get('_error'):
        return 'request_error', '', '', ''
    name = record.get('name', '')
    pattern = record.get('pattern') or record.get('luiPattern') or ''
    deprecated = record.get('deprecated', '')
    return 'found', name, pattern, deprecated


def main():
    repo_root = Path(__file__).parent.parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--patterns', default=str(Path(__file__).parent / 'data/togoid_extract_patterns.yaml'))
    parser.add_argument('--reconciliation', default=str(Path(__file__).parent / 'data/namespace_reconciliation.yaml'))
    parser.add_argument('--bulk', default=str(repo_root / 'data/identifiers_registry/idorg_resolver_dataset.json'),
                         help='Offline Identifiers.org export, used ahead of the live API')
    parser.add_argument('--cache', required=True, help='Registry-response cache directory')
    parser.add_argument('--output', required=True, help='TSV reconciliation report')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--apply', action='store_true',
                         help='Write newly-resolved identifiers.org URIs back into --patterns')
    args = parser.parse_args()

    pattern_data = yaml.safe_load(Path(args.patterns).read_text()) or {}
    rules = yaml.safe_load(Path(args.reconciliation).read_text()) or {}
    overrides = rules.get('dataset_prefixes', {})
    mapping_types = rules.get('mapping_types', {})
    not_registered = set(rules.get('not_registered', []))
    bulk_index = load_bulk_registry(args.bulk)
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    rows = []
    newly_resolved = []

    for layer in ('safe', 'context_required', 'excluded'):
        for key, spec in sorted((pattern_data.get(layer) or {}).items()):
            uri = str(spec.get('uri') or '')
            explicit = prefix_from_uri(uri)
            prefix = overrides.get(key) or explicit or (key if key in bulk_index else None)
            if key in overrides:
                source = 'reviewed_override'
            elif explicit:
                source = 'explicit_pattern_uri'
            elif prefix:
                source = 'registry_prefix_match'
            else:
                source = 'unresolved'
            status, name, registry_pattern, deprecated = ('not_checked', '', '', '')
            if prefix:
                status, name, registry_pattern, deprecated = registry_fields(
                    registry_record(prefix, cache, args.refresh, bulk_index))
            if prefix and not uri and source in ('reviewed_override', 'registry_prefix_match'):
                uri = f'http://identifiers.org/{prefix}/'
                newly_resolved.append((layer, key, uri))
            if key in not_registered:
                mapping_type = 'not_registered'
                review_note = 'Reviewed: no corresponding Identifiers.org namespace'
            elif prefix:
                mapping_type = mapping_types.get(key, 'direct')
                review_note = ''
            else:
                mapping_type = 'needs_review'
                review_note = 'No explicit URI or reviewed mapping'
            rows.append({
                'dataset_key': key,
                'pattern_layer': layer,
                'category': spec.get('category', ''),
                'extraction_pattern': spec.get('pattern') or spec.get('orig_regex') or '',
                'registry_prefix': prefix or '',
                'mapping_type': mapping_type,
                'mapping_source': source,
                'registry_status': status,
                'registry_name': name,
                'registry_pattern': registry_pattern,
                'registry_deprecated': deprecated,
                'current_uri': uri,
                'review_note': review_note,
            })

    if args.apply and newly_resolved:
        for layer, key, uri in newly_resolved:
            pattern_data[layer][key]['uri'] = uri
        Path(args.patterns).write_text(
            yaml.safe_dump(pattern_data, sort_keys=False, allow_unicode=True, width=4096))

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, delimiter='\t')
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        'total': len(rows),
        'registry_found': sum(row['registry_status'] == 'found' for row in rows),
        'needs_review': sum(row['mapping_type'] == 'needs_review' for row in rows),
        'not_registered': sum(row['mapping_type'] == 'not_registered' for row in rows),
        'registry_deprecated': sum(row['registry_deprecated'] is True for row in rows),
        'newly_resolved': len(newly_resolved),
        'applied': bool(args.apply and newly_resolved),
    }
    output.with_suffix('.summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
