import copy
import json
from tests.support import Case

ORIGIN = 'http://partner.test'
PREFIX = '/approved/'
ENTRY = ORIGIN + '/approved/catalog'

CATALOG = {
    '@graph': [
        {
            '@id': '/approved/asset/a',
            '@type': 'Asset',
            'title': '合成观测集',
            'distribution': {'@id': '/approved/distribution/a'},
            'grant': {'@id': '/approved/license/a'},
            'asset_id': 'remote-a',
            'kind': 'source',
            'owner': 'synthetic-owner',
            'columns': [{'name': 'text', 'type': 'string', 'classification': 'internal', 'nullable': False}],
        },
        {'@id': '_:note', 'text': '仅本页备注'},
    ]
}
LICENSE = {
    '@id': '/approved/license/a',
    '@type': 'Grant',
    'asset': {'@id': '/approved/asset/a'},
    'purposes': ['research'],
    'regions': ['CN'],
    'expires_at': '2027-11-01T00:00:00Z',
    'grant_id': 'remote-a-grant',
    'rights': ['read'],
    'valid_from': '2026-01-01T00:00:00Z',
    'obligations': ['attribution'],
    'columns': ['text'],
    'max_depth': 3,
    'allow_derivatives': False,
}
DISTRIBUTION = {
    '@id': '/approved/distribution/a',
    '@type': 'Distribution',
    'asset': {'@id': '/approved/asset/a'},
    'download': '/approved/data/a',
    'format': 'csv',
}


def pages():
    return {
        ENTRY: {'json': copy.deepcopy(CATALOG)},
        ORIGIN + '/approved/asset/a': {'json': copy.deepcopy(CATALOG)},
        ORIGIN + '/approved/license/a': {'json': copy.deepcopy(LICENSE)},
        ORIGIN + '/approved/distribution/a': {'json': copy.deepcopy(DISTRIBUTION)},
    }


def transport(served, calls=None):
    def get(url, byte_cap):
        if calls is not None:
            calls.append(url)
        page = served.get(url)
        if page is None:
            return {'status': 404, 'headers': {'content-type': 'application/json'}, 'body': b'{}', 'truncated': False, 'error': None}
        if 'location' in page:
            return {'status': page.get('status', 302), 'headers': {'location': page['location']}, 'body': b'', 'truncated': False, 'error': None}
        body = json.dumps(page.get('json', {}), ensure_ascii=False, sort_keys=True).encode()
        truncated = len(body) > byte_cap
        return {'status': page.get('status', 200), 'headers': {'content-type': page.get('type', 'application/json')}, 'body': body[:byte_cap], 'truncated': truncated, 'error': None}
    return get


class HarvestCase(Case):
    def register(self, source_id='partner', entry=ENTRY, max_documents=10, max_bytes=65536, origin=ORIGIN, prefix=PREFIX):
        return self.app.harvest.register(source_id, entry, origin, prefix, max_documents, max_bytes)

    def ensure(self, source_id='partner'):
        from rights_graph.primitives import GraphError
        try:
            self.app.harvest.source(source_id)
        except GraphError:
            self.register(source_id)

    def collect(self, served=None, source_id='partner', calls=None):
        self.ensure(source_id)
        return self.app.harvest.run(source_id, transport(served if served is not None else pages(), calls))

    def only_candidate(self, served=None, calls=None):
        self.collect(served, calls=calls)
        rows = self.app.harvest.candidates('partner')
        self.assertEqual(len(rows), 1)
        return self.app.harvest.candidate(rows[0]['candidate_id'])

    def confirm(self, candidate, reviewer='reviewer-li'):
        return self.app.harvest.confirm(candidate['candidate_id'], reviewer, '证据齐全', candidate['evidence_digest'], candidate['catalog_version'])
