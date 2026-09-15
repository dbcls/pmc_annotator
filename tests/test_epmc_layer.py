import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from pmc_annotator.epmc_annotations import (
    normalize, annotations, identifiers_namespace, dynamic_dataset, locate,
)
from pmc_annotator.togoid_annotator import TogoIDAnnotator
from pmc_annotator.compare_extractions import merge, compare


class EpmcLayerTest(unittest.TestCase):
    def test_import_merge_compare(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            xml = root / 'PMC123.xml'
            xml.write_text('<article><front><article-meta><article-id pub-id-type="pmc">123</article-id></article-meta></front><body><sec><p>We used GSE12345 and 10.1000/example.</p></sec></body></article>')
            plan = root / 'plan.json'
            plan.write_text(json.dumps({'shards': [{'shard_id': '00000', 'files': [str(xml)]}]}))
            raw = root / 'raw'
            raw.mkdir()
            def ann(kind, exact):
                return {'type': kind, 'body': 'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE12345',
                        'target': {'selector': {'exact': exact}}}
            (raw / 'response.json').write_text(json.dumps({'doc_id': 'PMC123',
                'requested_article_ids': ['PMC:123'], 'response': [
                    ann('Accession Numbers', 'GSE12345'),
                    ann('Gene_Proteins', 'GSE12345'),
                    ann('Accession Numbers', 'GSM999'),
                    {'type': 'Accession Numbers', 'memberOf': 'DOI',
                     'body': 'https://identifiers.org/doi:10.1000/example',
                     'target': {'selector': {'exact': '10.1000/example'}}}]}))
            mapping = Path(__file__).parents[1] / 'src/pmc_annotator/data/epmc_mapping.yaml'
            normalize(SimpleNamespace(plan=plan, raw=raw, mapping=mapping, output=root / 'epmc'))
            epmc = pd.read_parquet(root / 'epmc/shard_epmc.parquet')
            self.assertEqual(len(epmc), 1)
            self.assertEqual(epmc.loc[epmc.db == 'geo_series', 'offset'].iloc[0], 8)
            self.assertEqual(len(json.loads((root / 'epmc/rejected.json').read_text())), 1)
            external = pd.read_parquet(root / 'epmc/external_identifiers.parquet')
            self.assertEqual(len(external), 1)
            self.assertEqual(external.iloc[0].db, 'doi')
            regex = epmc.copy()
            regex['source'] = 'regex_togoid'
            regex['confidence'] = 'high'
            merge(regex, epmc, root)
            merged = pd.read_parquet(root / 'shard_merged.parquet')
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged.iloc[0].source, 'europepmc,regex_togoid')
            compare(regex, epmc, root)
            self.assertEqual(json.loads((root / 'summary.json').read_text())['both'], 1)

    def test_response_error_is_not_empty(self):
        self.assertEqual(annotations([]), [])
        with self.assertRaises(ValueError):
            annotations({'error': 'temporarily unavailable'})

    def test_identifiers_org_selects_togoid_dataset(self):
        patterns = Path(__file__).parents[1] / 'src/pmc_annotator/data/togoid_extract_patterns.yaml'
        resolver = TogoIDAnnotator(patterns)
        self.assertEqual(
            identifiers_namespace('https://identifiers.org/geo:GSE80606'), 'geo')
        self.assertEqual(dynamic_dataset('GSE80606', 'geo', resolver), 'geo_series')
        self.assertEqual(dynamic_dataset('GSM80606', 'geo', resolver), 'geo_sample')
        self.assertEqual(
            identifiers_namespace('https://identifiers.org/ebi/bioproject:PRJNA715749'),
            'bioproject')

    def test_context_alignment_normalises_nonbreaking_space(self):
        first = SimpleNamespace(text='GSE1 [1], Toomey et al.', offset=0, infon={})
        second = SimpleNamespace(text='GSE1 [2], Different context.', offset=40, infon={})
        doc = SimpleNamespace(passages=[first, second])
        hits = locate(doc, 'GSE1', {
            'prefix': '', 'suffix': ' [1], Toomey et\u00a0al.'})
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], 0)

    def test_context_alignment_uses_epmc_section(self):
        methods = SimpleNamespace(text='GSE1 87 (48 R, 39 NR)37 ', offset=0,
                                  infon={'section_type': 'methods'})
        results = SimpleNamespace(text='734330489262357 GSE1 87 (48 R, 39 NR)37 ',
                                  offset=40, infon={'section_type': 'results'})
        doc = SimpleNamespace(passages=[methods, results])
        hits = locate(doc, 'GSE1', {
            'prefix': '734330489262357 ', 'suffix': ' 87 (48 R, 39 NR)37\ufffd'},
            'Results')
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], 1)

    def test_context_alignment_falls_back_when_section_vocabularies_differ(self):
        body = SimpleNamespace(text='project accession numbers PRJNA1398387 and PRJNA715749.',
                               offset=0, infon={'section_type': 'body'})
        doc = SimpleNamespace(passages=[body])
        hits = locate(doc, 'PRJNA1398387', {
            'prefix': 'ect accession numbers ', 'suffix': ' and PRJNA715749.'},
            'Data Availability')
        self.assertEqual(len(hits), 1)


if __name__ == '__main__':
    unittest.main()
