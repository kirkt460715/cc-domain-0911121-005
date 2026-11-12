import json
from tests.support import Case
from rights_graph.harvest.jsonld import parse_document

PAGE = 'http://partner.test/approved/catalog'


class JsonldTests(Case):
    def parse(self, value, page=PAGE):
        return parse_document(json.dumps(value, ensure_ascii=False), page)

    def test_relative_ids_resolve_against_page_url(self):
        result = self.parse({'@id': 'asset/a', '@type': 'Asset', 'grant': {'@id': '../license/a'}})
        self.assertEqual(result['nodes'][0]['id'], 'http://partner.test/approved/asset/a')
        self.assertEqual(result['links'], ['http://partner.test/license/a'])
        self.assertEqual(result['nodes'][0]['fields']['grant'], {'@id': 'http://partner.test/license/a'})

    def test_graph_items_and_references(self):
        result = self.parse({'@graph': [
            {'@id': '/approved/asset/a', '@type': 'Asset', 'title': '合成观测集'},
            {'@id': '/approved/license/a'},
        ]})
        self.assertEqual([node['id'] for node in result['nodes']], ['http://partner.test/approved/asset/a'])
        self.assertEqual(result['links'], ['http://partner.test/approved/license/a'])

    def test_embedded_node_registers_and_parent_keeps_reference(self):
        result = self.parse({'@id': '/approved/asset/a', '@type': 'Asset', 'grant': {'@id': '/approved/license/a', '@type': 'Grant', 'purposes': ['research']}})
        self.assertEqual(result['links'], [])
        self.assertEqual([node['type'] for node in result['nodes']], ['Grant', 'Asset'])
        grant = next(node for node in result['nodes'] if node['type'] == 'Grant')
        self.assertEqual(grant['fields']['purposes'], ['research'])

    def test_blank_nodes_stay_document_local(self):
        result = self.parse({'@graph': [{'@id': '_:note', 'text': '仅本页备注'}, {'@id': '/approved/asset/a', '@type': 'Asset', 'remark': {'@id': '_:note'}}]})
        self.assertEqual(result['blank_nodes'], 1)
        self.assertEqual(result['links'], [])
        blank = next(node for node in result['nodes'] if node['blank'])
        self.assertIsNone(blank['id'])
        asset = next(node for node in result['nodes'] if not node['blank'])
        self.assertEqual(asset['fields']['remark'], {'@id': '_:note'})

    def test_context_is_never_fetched_and_unknown_type_not_inferred(self):
        result = self.parse({'@context': 'http://partner.test/context.jsonld', '@id': '/approved/x', '@type': 'Contract', 'signature': 'abc'})
        self.assertTrue(result['context_ignored'])
        self.assertEqual(result['unknown_types'], ['Contract'])
        self.assertEqual(result['links'], [])

    def test_scalar_arrays_and_value_objects_are_kept(self):
        result = self.parse({'@id': '/approved/license/a', '@type': 'Grant', 'purposes': ['research'], 'columns': ['text'], 'max_depth': 3, 'allow_derivatives': False})
        fields = result['nodes'][0]['fields']
        self.assertEqual(fields['purposes'], ['research'])
        self.assertEqual(fields['max_depth'], 3)
        self.assertFalse(fields['allow_derivatives'])

    def test_malformed_documents_are_rejected(self):
        self.fails('jsonld_document', parse_document, '[1,2]', PAGE)
        self.fails('jsonld_graph', parse_document, '{"@graph": {}}', PAGE)
        self.fails('jsonld_id', parse_document, '{"@id": 7}', PAGE)
        self.fails('jsonld_type', parse_document, '{"@type": ["Asset"]}', PAGE)
        self.fails('malformed_json', parse_document, '{not json', PAGE)
        self.fails('duplicate_key', parse_document, '{"@id": "/a", "@id": "/b"}', PAGE)
