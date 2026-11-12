import hashlib
import json
from urllib.parse import urljoin
from rights_graph.primitives import canonical, checksum, count, label, name, reject
from rights_graph.catalog import assets
from rights_graph.policy import grants
from rights_graph.journal import audit
from rights_graph.harvest.jsonld import parse_document
from rights_graph.harvest.fetch import http_get, in_scope, json_content, normalize_origin, normalize_prefix, REDIRECT_STATUSES

SCHEMA = '''
CREATE TABLE IF NOT EXISTS harvest_sources(
 source_id TEXT PRIMARY KEY,entry_url TEXT NOT NULL,origin TEXT NOT NULL,path_prefix TEXT NOT NULL,
 max_documents INTEGER NOT NULL,max_bytes INTEGER NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS harvest_runs(
 run_id INTEGER PRIMARY KEY AUTOINCREMENT,source_id TEXT NOT NULL REFERENCES harvest_sources(source_id),
 state TEXT NOT NULL,documents INTEGER NOT NULL,bytes INTEGER NOT NULL,summary TEXT NOT NULL,
 started_at TEXT NOT NULL,finished_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS harvest_documents(
 source_id TEXT NOT NULL REFERENCES harvest_sources(source_id),url TEXT NOT NULL,
 run_id INTEGER NOT NULL REFERENCES harvest_runs(run_id),status TEXT NOT NULL,detail TEXT,
 sha256 TEXT,bytes INTEGER,final_url TEXT,body TEXT,fetched_at TEXT NOT NULL,
 PRIMARY KEY(source_id,url));
CREATE TABLE IF NOT EXISTS harvest_candidates(
 candidate_id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES harvest_sources(source_id),
 node_id TEXT NOT NULL,state TEXT NOT NULL,evidence TEXT NOT NULL,missing TEXT NOT NULL,
 conflicts TEXT NOT NULL,notices TEXT NOT NULL,resolutions TEXT NOT NULL,complete INTEGER NOT NULL,
 evidence_digest TEXT NOT NULL,catalog_version TEXT NOT NULL,run_id INTEGER NOT NULL,
 applied_asset TEXT,applied_grant TEXT,applied_confirmation INTEGER,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(source_id,node_id));
CREATE TABLE IF NOT EXISTS harvest_confirmations(
 candidate_id TEXT NOT NULL REFERENCES harvest_candidates(candidate_id),ordinal INTEGER NOT NULL,
 reviewer TEXT NOT NULL,reason TEXT NOT NULL,evidence_digest TEXT NOT NULL,catalog_version TEXT NOT NULL,
 decided_at TEXT NOT NULL,PRIMARY KEY(candidate_id,ordinal));
'''

ASSET_FIELDS = ('asset_id', 'kind', 'owner', 'columns', 'title')
GRANT_FIELDS = ('grant_id', 'purposes', 'regions', 'rights', 'valid_from', 'expires_at', 'obligations', 'columns', 'max_depth', 'allow_derivatives')
DISTRIBUTION_FIELDS = ('download', 'format')
STRUCTURAL_LINKS = ('asset', 'distribution', 'grant')
RESOLVABLE_FIELDS = {'asset.' + field for field in ASSET_FIELDS + ('grant',)} | {'grant.' + field for field in GRANT_FIELDS}
MAX_REDIRECTS = 5
PENDING_LIMIT = 500


class HarvestDesk:
    """Collects approved catalog entries and prepares reviewed candidates.

    Collection never touches local assets, grants or recipes. A candidate only
    becomes a local asset and grant after a reviewer confirms the assembled
    evidence group and catalog version, and the apply step goes through the
    existing catalog and policy modules with their validation unchanged.
    """

    def __init__(self, store):
        self.store = store
        if store.depth:
            reject('transaction_scope', 'Initialize harvest desk outside transaction')
        store.db.executescript(SCHEMA)

    # ---------- approved sources ----------

    def register(self, source_id, entry_url, origin, path_prefix, max_documents, max_bytes):
        name(source_id, 'source_id')
        origin = normalize_origin(origin)
        path_prefix = normalize_prefix(path_prefix)
        count(max_documents, 'max_documents', 1, 1000)
        count(max_bytes, 'max_bytes', 1, 2**31)
        if not isinstance(entry_url, str) or not in_scope(origin, path_prefix, entry_url):
            reject('harvest_entry_scope', 'Entry URL must stay inside the approved origin and path prefix')
        with self.store.atomic():
            if self.store.one('SELECT source_id FROM harvest_sources WHERE source_id=?', (source_id,)):
                reject('harvest_source_exists', 'Source identifier already registered')
            self.store.execute(
                "INSERT INTO harvest_sources(source_id,entry_url,origin,path_prefix,max_documents,max_bytes,state,created_at) VALUES(?,?,?,?,?,?,'active',?)",
                (source_id, entry_url, origin, path_prefix, max_documents, max_bytes, self.store.clock()))
            audit.append(self.store, 'harvest.source_registered', {'source_id': source_id, 'entry_url': entry_url, 'origin': origin, 'path_prefix': path_prefix})
            return self.source(source_id)

    def update(self, source_id, *, max_documents=None, max_bytes=None, state=None):
        with self.store.atomic():
            row = self.source_row(source_id)
            documents = row['max_documents'] if max_documents is None else count(max_documents, 'max_documents', 1, 1000)
            budget = row['max_bytes'] if max_bytes is None else count(max_bytes, 'max_bytes', 1, 2**31)
            state = row['state'] if state is None else state
            if state not in ('active', 'suspended'):
                reject('harvest_source_state', 'Source state must be active or suspended')
            self.store.execute('UPDATE harvest_sources SET max_documents=?,max_bytes=?,state=? WHERE source_id=?', (documents, budget, state, source_id))
            audit.append(self.store, 'harvest.source_updated', {'source_id': source_id, 'max_documents': documents, 'max_bytes': budget, 'state': state})
            return self.source(source_id)

    def source_row(self, source_id):
        name(source_id, 'source_id')
        row = self.store.one('SELECT * FROM harvest_sources WHERE source_id=?', (source_id,))
        if row is None:
            reject('unknown_harvest_source', 'Approved source does not exist', source_id=source_id)
        return row

    def source(self, source_id):
        row = self.source_row(source_id)
        latest = self.store.one('SELECT run_id FROM harvest_runs WHERE source_id=? ORDER BY run_id DESC LIMIT 1', (source_id,))
        row['latest_run'] = self.run_view(latest['run_id']) if latest else None
        row['candidates'] = {r['state']: r['count'] for r in self.store.all('SELECT state,COUNT(*) AS count FROM harvest_candidates WHERE source_id=? GROUP BY state ORDER BY state', (source_id,))}
        return row

    def sources(self):
        return [self.source(row['source_id']) for row in self.store.all('SELECT source_id FROM harvest_sources ORDER BY source_id')]

    # ---------- collection run ----------

    def run(self, source_id, transport=None):
        source = self.source_row(source_id)
        if source['state'] != 'active':
            reject('harvest_source_state', 'Suspended source cannot be collected')
        if not in_scope(source['origin'], source['path_prefix'], source['entry_url']):
            reject('harvest_entry_scope', 'Entry URL no longer stays inside the approved scope')
        started = self.store.clock()
        outcome = self.crawl(source, transport or http_get)
        finished = self.store.clock()
        with self.store.atomic():
            cursor = self.store.execute(
                'INSERT INTO harvest_runs(source_id,state,documents,bytes,summary,started_at,finished_at) VALUES(?,?,?,?,?,?,?)',
                (source_id, outcome['state'], len(outcome['documents']), outcome['bytes'], canonical(outcome['summary']), started, finished))
            run_id = cursor.lastrowid
            for doc in outcome['documents']:
                self.store.execute(
                    'INSERT OR REPLACE INTO harvest_documents(source_id,url,run_id,status,detail,sha256,bytes,final_url,body,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (source_id, doc['url'], run_id, doc['status'], doc['detail'], doc['sha256'], doc['bytes'], doc['final_url'], doc['body'], finished))
            candidates = self.assemble(source_id, run_id)
            summary = dict(outcome['summary'])
            summary['candidates'] = candidates
            self.store.execute('UPDATE harvest_runs SET summary=? WHERE run_id=?', (canonical(summary), run_id))
            audit.append(self.store, 'harvest.run_completed', {
                'source_id': source_id, 'run_id': run_id, 'state': outcome['state'],
                'documents': len(outcome['documents']), 'bytes': outcome['bytes'],
                'pending': summary['pending_count'], 'exhausted': summary['exhausted']})
            return self.run_view(run_id)

    def crawl(self, source, get):
        origin, path_prefix = source['origin'], source['path_prefix']
        queue = [source['entry_url']]
        seen = set()
        documents = []
        pending = []
        out_of_scope = []
        unknown_types = []
        blank_nodes = 0
        bytes_used = 0
        exhausted = None
        while queue:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if len(documents) >= source['max_documents'] or bytes_used >= source['max_bytes']:
                exhausted = exhausted or ('documents' if len(documents) >= source['max_documents'] else 'bytes')
                pending.append(url)
                continue
            document = None
            current = url
            chain = []
            for _ in range(MAX_REDIRECTS + 1):
                response = get(current, source['max_bytes'] - bytes_used)
                bytes_used += len(response['body'])
                if response['error'] is not None:
                    document = self.document(url, 'error', 'transport', bytes_used=0)
                    break
                if response['truncated']:
                    exhausted = exhausted or 'bytes'
                    pending.append(url)
                    break
                status = response['status']
                if status in REDIRECT_STATUSES:
                    location = response['headers'].get('location')
                    if not location:
                        document = self.document(url, 'error', 'redirect_without_location')
                        break
                    target = urljoin(current, location)
                    if not in_scope(origin, path_prefix, target):
                        document = self.document(url, 'blocked', 'redirect_out_of_scope', final_url=target)
                        break
                    if target in chain or target == url:
                        document = self.document(url, 'error', 'redirect_loop')
                        break
                    chain.append(target)
                    current = target
                    continue
                document = self.terminal_document(url, current, status, response)
                break
            else:
                document = self.document(url, 'error', 'redirect_limit')
            if document is None:
                continue
            documents.append(document)
            if document['status'] != 'fetched':
                continue
            parsed = document['parsed']
            blank_nodes += parsed['blank_nodes']
            for item in parsed['unknown_types']:
                if item not in unknown_types:
                    unknown_types.append(item)
            for link in parsed['links']:
                if in_scope(origin, path_prefix, link):
                    queue.append(link)
                else:
                    out_of_scope.append({'url': link, 'from': document['url']})
        state = 'incomplete' if exhausted else 'complete'
        summary = {
            'entry': source['entry_url'],
            'budget': {'max_documents': source['max_documents'], 'max_bytes': source['max_bytes']},
            'exhausted': exhausted,
            'documents': [{key: doc[key] for key in ('url', 'status', 'detail', 'sha256', 'bytes', 'final_url')} for doc in documents],
            'pending': pending[:PENDING_LIMIT],
            'pending_count': len(pending),
            'out_of_scope': out_of_scope[:PENDING_LIMIT],
            'unknown_types': sorted(unknown_types),
            'blank_nodes': blank_nodes,
        }
        return {'state': state, 'documents': documents, 'bytes': bytes_used, 'summary': summary}

    def document(self, url, status, detail=None, final_url=None, bytes_used=0):
        return {'url': url, 'status': status, 'detail': detail, 'sha256': None, 'bytes': bytes_used, 'final_url': final_url or url, 'body': None, 'parsed': None}

    def terminal_document(self, url, final_url, status, response):
        size = len(response['body'])
        if status == 404:
            return self.document(url, 'missing', 'not_found', final_url=final_url, bytes_used=size)
        if status != 200:
            return self.document(url, 'error', 'http_' + str(status), final_url=final_url, bytes_used=size)
        if not json_content(response['headers']):
            return self.document(url, 'unsupported', 'content_type', final_url=final_url, bytes_used=size)
        try:
            text = response['body'].decode('utf-8')
        except UnicodeDecodeError:
            return self.document(url, 'unsupported', 'encoding', final_url=final_url, bytes_used=size)
        from rights_graph.primitives import GraphError
        try:
            parsed = parse_document(text, final_url)
        except GraphError as exc:
            return self.document(url, 'unsupported', exc.code, final_url=final_url, bytes_used=size)
        document = self.document(url, 'fetched', final_url=final_url, bytes_used=size)
        document['sha256'] = hashlib.sha256(response['body']).hexdigest()
        document['body'] = text
        document['parsed'] = parsed
        return document

    # ---------- candidate assembly ----------

    def run_index(self, source_id, run_id):
        index = {}
        for doc in self.store.all("SELECT * FROM harvest_documents WHERE source_id=? AND run_id=? AND status='fetched' ORDER BY url", (source_id, run_id)):
            parsed = parse_document(doc['body'], doc['final_url'])
            for node in parsed['nodes']:
                if node['id'] is not None and not node['blank']:
                    index.setdefault(node['id'], []).append({'doc': doc, 'node': node})
        return index

    def document_state(self, source_id, run_id, url, summary):
        row = self.store.one('SELECT status FROM harvest_documents WHERE source_id=? AND url=? AND run_id=?', (source_id, url, run_id))
        if row is not None:
            return row['status']
        if url in summary.get('pending', []):
            return 'pending'
        if any(item['url'] == url for item in summary.get('out_of_scope', [])):
            return 'out_of_scope'
        return 'not_requested'

    def assemble(self, source_id, run_id, only_node=None):
        index = self.run_index(source_id, run_id)
        run = self.store.one('SELECT summary FROM harvest_runs WHERE run_id=?', (run_id,))
        summary = json.loads(run['summary'])
        existing = {row['node_id']: row for row in self.store.all('SELECT * FROM harvest_candidates WHERE source_id=?', (source_id,))}
        node_ids = {node_id for node_id, occurrences in index.items() if any(item['node']['type'] == 'Asset' for item in occurrences)}
        node_ids |= set(existing)
        if only_node is not None:
            node_ids &= {only_node}
        results = []
        for node_id in sorted(node_ids):
            results.append(self.assemble_candidate(source_id, run_id, node_id, index.get(node_id, []), existing.get(node_id), index, summary))
        return results

    def gather(self, occurrences, field, node_id):
        values = {}
        for item in occurrences:
            if field not in item['node']['fields']:
                continue
            value = item['node']['fields'][field]
            source = {'url': item['doc']['url'], 'node': node_id, 'sha256': item['doc']['sha256']}
            entry = values.setdefault(canonical(value), {'value': value, 'sources': []})
            if source not in entry['sources']:
                entry['sources'].append(source)
        return values

    def assemble_candidate(self, source_id, run_id, node_id, occurrences, existing, index, summary):
        resolutions = json.loads(existing['resolutions']) if existing else {}
        asset_nodes = [item for item in occurrences if item['node']['type'] == 'Asset']
        missing = []
        conflicts = []
        notices = []

        def effective(field_path, gathered):
            if len(gathered) == 1:
                only = next(iter(gathered.values()))
                return {'value': only['value'], 'origin': 'remote', 'sources': only['sources']}
            if field_path in resolutions:
                resolution = resolutions[field_path]
                return {'value': resolution['value'], 'origin': 'manual', 'sources': [], 'manual': {key: resolution[key] for key in ('reviewer', 'reason', 'at')}}
            if gathered:
                conflicts.append({'field': field_path, 'values': list(gathered.values())})
            else:
                missing.append({'field': field_path, 'reason': 'absent'})
            return None

        types = sorted({item['node']['type'] for item in occurrences if item['node']['type']})
        if len(types) > 1:
            conflicts.append({'field': 'asset.@type', 'values': [{'value': item['node']['type'], 'sources': [{'url': item['doc']['url'], 'node': node_id, 'sha256': item['doc']['sha256']}]} for item in occurrences if item['node']['type']]})

        asset_fields = {}
        for field in ASSET_FIELDS:
            resolved = effective('asset.' + field, self.gather(asset_nodes, field, node_id))
            if resolved is not None:
                asset_fields[field] = resolved
        grant_link = effective('asset.grant', self.gather(asset_nodes, 'grant', node_id))
        if asset_fields.get('kind', {}).get('value') not in (None, 'source'):
            notices.append({'code': 'kind_not_source', 'value': asset_fields['kind']['value']})

        grant_fields = None
        grant_url = grant_link['value'].get('@id') if grant_link and isinstance(grant_link['value'], dict) else None
        if grant_link is not None and grant_url is None:
            missing.append({'field': 'grant', 'reason': 'not_a_reference'})
        if grant_url is not None:
            grant_nodes = [item for item in index.get(grant_url, []) if item['node']['type'] == 'Grant']
            if not grant_nodes:
                missing.append({'field': 'grant', 'reason': 'evidence_not_collected', 'url': grant_url, 'document': self.document_state(source_id, run_id, grant_url, summary)})
            else:
                grant_types = sorted({item['node']['type'] for item in index.get(grant_url, []) if item['node']['type']})
                if len(grant_types) > 1:
                    conflicts.append({'field': 'grant.@type', 'values': [{'value': item['node']['type'], 'sources': [{'url': item['doc']['url'], 'node': grant_url, 'sha256': item['doc']['sha256']}]} for item in index[grant_url] if item['node']['type']]})
                grant_fields = {}
                for field in GRANT_FIELDS:
                    resolved = effective('grant.' + field, self.gather(grant_nodes, field, grant_url))
                    if resolved is not None:
                        grant_fields[field] = resolved

        distribution = None
        distribution_links = self.gather(asset_nodes, 'distribution', node_id)
        if len(distribution_links) == 1:
            link = next(iter(distribution_links.values()))['value']
            distribution_url = link.get('@id') if isinstance(link, dict) else None
            distribution = {'link': link, 'sources': next(iter(distribution_links.values()))['sources']}
            distribution_nodes = [item for item in index.get(distribution_url, []) if item['node']['type'] == 'Distribution'] if distribution_url else []
            if distribution_nodes:
                distribution['fields'] = {}
                for field in DISTRIBUTION_FIELDS:
                    gathered = self.gather(distribution_nodes, field, distribution_url)
                    if len(gathered) == 1:
                        distribution['fields'][field] = {'value': next(iter(gathered.values()))['value'], 'sources': next(iter(gathered.values()))['sources']}
                    elif gathered:
                        distribution.setdefault('conflicts', []).append({'field': 'distribution.' + field, 'values': list(gathered.values())})
            else:
                distribution['document'] = self.document_state(source_id, run_id, distribution_url, summary)
        elif len(distribution_links) > 1:
            distribution = {'conflicts': [{'field': 'asset.distribution', 'values': list(distribution_links.values())}]}

        unmapped = {
            'asset': sorted({key for item in asset_nodes for key in item['node']['fields']} - set(ASSET_FIELDS) - set(STRUCTURAL_LINKS)),
            'grant': sorted({key for item in (index.get(grant_url, []) if grant_url else []) if item['node']['type'] == 'Grant' for key in item['node']['fields']} - set(GRANT_FIELDS) - {'asset'}),
        }
        evidence = {'asset': asset_fields, 'grant_link': grant_link, 'grant': grant_fields, 'distribution': distribution, 'unmapped': unmapped}
        complete = not missing and not conflicts and not notices
        evidence_digest = checksum({'evidence': evidence, 'missing': missing, 'conflicts': conflicts, 'notices': notices})
        pages = {}
        for group in (evidence, {'conflicts': conflicts}):
            for sources in self.walk_sources(group):
                pages[(sources['url'], sources['sha256'])] = {'url': sources['url'], 'sha256': sources['sha256']}
        catalog_version = checksum(sorted(pages.values(), key=lambda item: item['url']))
        candidate_id = 'harvest-' + checksum({'source_id': source_id, 'node_id': node_id})[:24]
        now = self.store.clock()
        with self.store.atomic():
            if existing is None:
                state = 'pending'
                self.store.execute(
                    'INSERT INTO harvest_candidates(candidate_id,source_id,node_id,state,evidence,missing,conflicts,notices,resolutions,complete,evidence_digest,catalog_version,run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (candidate_id, source_id, node_id, state, canonical(evidence), canonical(missing), canonical(conflicts), canonical(notices), canonical(resolutions), complete, evidence_digest, catalog_version, run_id, now, now))
                audit.append(self.store, 'harvest.candidate_assembled', {'candidate_id': candidate_id, 'source_id': source_id, 'node_id': node_id, 'complete': bool(complete)})
            elif existing['evidence_digest'] != evidence_digest or existing['run_id'] != run_id:
                state = existing['state']
                if existing['evidence_digest'] != evidence_digest and state in ('confirmed', 'rejected'):
                    state = 'pending'
                self.store.execute(
                    'UPDATE harvest_candidates SET state=?,evidence=?,missing=?,conflicts=?,notices=?,resolutions=?,complete=?,evidence_digest=?,catalog_version=?,run_id=?,updated_at=? WHERE candidate_id=?',
                    (state, canonical(evidence), canonical(missing), canonical(conflicts), canonical(notices), canonical(resolutions), complete, evidence_digest, catalog_version, run_id, now, candidate_id))
        return {'candidate_id': candidate_id, 'node_id': node_id, 'state': self.store.one('SELECT state FROM harvest_candidates WHERE candidate_id=?', (candidate_id,))['state'], 'complete': bool(complete), 'missing': len(missing), 'conflicts': len(conflicts)}

    def walk_sources(self, value):
        if isinstance(value, dict):
            if set(value) >= {'url', 'sha256'}:
                yield value
            for item in value.values():
                yield from self.walk_sources(item)
        elif isinstance(value, list):
            for item in value:
                yield from self.walk_sources(item)

    # ---------- candidate review ----------

    def candidate_row(self, candidate_id):
        row = self.store.one('SELECT * FROM harvest_candidates WHERE candidate_id=?', (candidate_id,))
        if row is None:
            reject('unknown_harvest_candidate', 'Harvest candidate does not exist', candidate_id=candidate_id)
        return row

    def candidate(self, candidate_id):
        row = self.candidate_row(candidate_id)
        evidence = json.loads(row['evidence'])
        conflicts = json.loads(row['conflicts'])
        urls = {source['url'] for source in self.walk_sources({'evidence': evidence, 'conflicts': conflicts})}
        documents = {doc['url']: doc for doc in self.store.all('SELECT url,run_id,status,fetched_at FROM harvest_documents WHERE source_id=?', (row['source_id'],))}
        run_ids = sorted({documents[url]['run_id'] for url in urls if url in documents})

        def decorate(value):
            if isinstance(value, dict):
                if set(value) >= {'url', 'sha256'}:
                    document = documents.get(value['url'])
                    if document is not None:
                        return {**value, 'run_id': document['run_id'], 'fetched_at': document['fetched_at']}
                    return value
                return {key: decorate(item) for key, item in value.items()}
            if isinstance(value, list):
                return [decorate(item) for item in value]
            return value

        confirmations = [dict(item) for item in self.store.all('SELECT * FROM harvest_confirmations WHERE candidate_id=? ORDER BY ordinal', (candidate_id,))]
        latest = confirmations[-1] if confirmations else None
        applied_confirmation = None
        if row['applied_confirmation'] is not None:
            applied_confirmation = next((item for item in confirmations if item['ordinal'] == row['applied_confirmation']), None)
        return {
            'candidate_id': candidate_id,
            'source_id': row['source_id'],
            'node_id': row['node_id'],
            'state': row['state'],
            'complete': bool(row['complete']),
            'missing': json.loads(row['missing']),
            'conflicts': decorate(conflicts),
            'notices': json.loads(row['notices']),
            'fields': decorate(evidence),
            'resolutions': json.loads(row['resolutions']),
            'evidence_digest': row['evidence_digest'],
            'catalog_version': row['catalog_version'],
            'confirmations': confirmations,
            'changed_since_confirmation': bool(latest and latest['evidence_digest'] != row['evidence_digest']),
            'applied': {'asset_id': row['applied_asset'], 'grant_id': row['applied_grant'], 'confirmation': row['applied_confirmation']} if row['applied_asset'] else None,
            'changed_since_apply': bool(applied_confirmation and applied_confirmation['evidence_digest'] != row['evidence_digest']),
            'runs': {str(run_id): self.run_view(run_id) for run_id in run_ids},
            'updated_at': row['updated_at'],
        }

    def candidates(self, source_id):
        self.source_row(source_id)
        rows = self.store.all('SELECT candidate_id,node_id,state,complete,missing,conflicts,evidence_digest,catalog_version FROM harvest_candidates WHERE source_id=? ORDER BY node_id', (source_id,))
        for row in rows:
            row['missing'] = len(json.loads(row['missing']))
            row['conflicts'] = len(json.loads(row['conflicts']))
            row['complete'] = bool(row['complete'])
        return rows

    def resolve(self, candidate_id, field, value, reviewer, reason):
        name(reviewer, 'reviewer')
        reason = label(reason, 'reason')
        if field not in RESOLVABLE_FIELDS:
            reject('harvest_resolution_field', 'Field cannot be manually resolved', field=field)
        canonical(value)
        with self.store.atomic():
            row = self.candidate_row(candidate_id)
            if row['state'] != 'pending':
                reject('harvest_candidate_state', 'Only pending candidates accept manual resolution')
            missing = {item['field'] for item in json.loads(row['missing'])}
            conflicts = {item['field']: item for item in json.loads(row['conflicts'])}
            if field in conflicts:
                observed = {canonical(item['value']) for item in conflicts[field]['values']}
                if canonical(value) not in observed:
                    reject('harvest_resolution_value', 'Conflict resolution must choose one of the collected values')
            elif field not in missing:
                reject('harvest_resolution_inapplicable', 'Field is already provided by collected evidence')
            resolutions = json.loads(row['resolutions'])
            resolutions[field] = {'value': value, 'reviewer': reviewer, 'reason': reason, 'at': self.store.clock()}
            self.store.execute('UPDATE harvest_candidates SET resolutions=? WHERE candidate_id=?', (canonical(resolutions), candidate_id))
            audit.append(self.store, 'harvest.candidate_resolved', {'candidate_id': candidate_id, 'field': field, 'reviewer': reviewer})
            self.assemble(row['source_id'], row['run_id'], only_node=row['node_id'])
            return self.candidate(candidate_id)

    def confirm(self, candidate_id, reviewer, reason, expected_evidence_digest, expected_catalog_version):
        name(reviewer, 'reviewer')
        reason = label(reason, 'reason')
        with self.store.atomic():
            row = self.candidate_row(candidate_id)
            if row['state'] != 'pending':
                reject('harvest_candidate_state', 'Only pending candidates can be confirmed')
            if not row['complete']:
                reject('harvest_incomplete', 'Candidate evidence is incomplete', missing=json.loads(row['missing']), conflicts=json.loads(row['conflicts']), notices=json.loads(row['notices']))
            if expected_evidence_digest != row['evidence_digest'] or expected_catalog_version != row['catalog_version']:
                reject('harvest_evidence_stale', 'Evidence changed since the reviewer snapshot; review again')
            ordinal = self.store.scalar('SELECT COUNT(*) FROM harvest_confirmations WHERE candidate_id=?', (candidate_id,)) + 1
            self.store.execute(
                'INSERT INTO harvest_confirmations(candidate_id,ordinal,reviewer,reason,evidence_digest,catalog_version,decided_at) VALUES(?,?,?,?,?,?,?)',
                (candidate_id, ordinal, reviewer, reason, row['evidence_digest'], row['catalog_version'], self.store.clock()))
            self.store.execute("UPDATE harvest_candidates SET state='confirmed',updated_at=? WHERE candidate_id=?", (self.store.clock(), candidate_id))
            audit.append(self.store, 'harvest.candidate_confirmed', {'candidate_id': candidate_id, 'reviewer': reviewer, 'evidence_digest': row['evidence_digest'], 'catalog_version': row['catalog_version']})
            return self.candidate(candidate_id)

    def reopen(self, candidate_id, reviewer, reason):
        name(reviewer, 'reviewer')
        reason = label(reason, 'reason')
        with self.store.atomic():
            row = self.candidate_row(candidate_id)
            if row['state'] not in ('confirmed', 'rejected'):
                reject('harvest_candidate_state', 'Only confirmed or rejected candidates can be reopened')
            self.store.execute("UPDATE harvest_candidates SET state='pending',updated_at=? WHERE candidate_id=?", (self.store.clock(), candidate_id))
            audit.append(self.store, 'harvest.candidate_reopened', {'candidate_id': candidate_id, 'reviewer': reviewer, 'reason': reason})
            return self.candidate(candidate_id)

    def reject_candidate(self, candidate_id, reviewer, reason):
        name(reviewer, 'reviewer')
        reason = label(reason, 'reason')
        with self.store.atomic():
            row = self.candidate_row(candidate_id)
            if row['state'] != 'pending':
                reject('harvest_candidate_state', 'Only pending candidates can be rejected')
            self.store.execute("UPDATE harvest_candidates SET state='rejected',updated_at=? WHERE candidate_id=?", (self.store.clock(), candidate_id))
            audit.append(self.store, 'harvest.candidate_rejected', {'candidate_id': candidate_id, 'reviewer': reviewer, 'reason': reason})
            return self.candidate(candidate_id)

    def apply(self, candidate_id, operator):
        name(operator, 'operator')
        with self.store.atomic():
            row = self.candidate_row(candidate_id)
            if row['state'] != 'confirmed':
                reject('harvest_not_confirmed', 'Candidate must be confirmed before it can be applied')
            confirmation = self.store.one('SELECT * FROM harvest_confirmations WHERE candidate_id=? ORDER BY ordinal DESC LIMIT 1', (candidate_id,))
            if confirmation is None or confirmation['evidence_digest'] != row['evidence_digest']:
                reject('harvest_evidence_stale', 'Evidence changed after confirmation; review again')
            stale = [source['url'] for source in self.walk_sources(json.loads(row['evidence']))
                     if (self.store.one('SELECT sha256,status FROM harvest_documents WHERE source_id=? AND url=?', (row['source_id'], source['url'])) or {}).get('sha256') != source['sha256']]
            if stale:
                reject('harvest_evidence_stale', 'Collected pages changed after confirmation; review again', urls=sorted(stale))
            evidence = json.loads(row['evidence'])

            def value(group, field):
                return evidence[group][field]['value']

            metadata = {'harvest': {
                'source_id': row['source_id'],
                'candidate_id': candidate_id,
                'node_id': row['node_id'],
                'evidence_digest': row['evidence_digest'],
                'catalog_version': row['catalog_version'],
            }}
            asset = assets.create(self.store, value('asset', 'asset_id'), value('asset', 'kind'), value('asset', 'owner'), value('asset', 'columns'), value('asset', 'title'), metadata=metadata)
            policy = {
                'purposes': value('grant', 'purposes'),
                'regions': value('grant', 'regions'),
                'rights': value('grant', 'rights'),
                'valid_from': value('grant', 'valid_from'),
                'valid_until': value('grant', 'expires_at'),
                'obligations': value('grant', 'obligations'),
                'columns': value('grant', 'columns'),
                'max_depth': value('grant', 'max_depth'),
                'allow_derivatives': value('grant', 'allow_derivatives'),
            }
            grant = grants.create(self.store, value('grant', 'grant_id'), asset['asset_id'], policy)
            self.store.execute(
                "UPDATE harvest_candidates SET state='applied',applied_asset=?,applied_grant=?,applied_confirmation=?,updated_at=? WHERE candidate_id=?",
                (asset['asset_id'], grant['grant_id'], confirmation['ordinal'], self.store.clock(), candidate_id))
            audit.append(self.store, 'harvest.candidate_applied', {
                'candidate_id': candidate_id, 'asset_id': asset['asset_id'], 'grant_id': grant['grant_id'],
                'evidence_digest': row['evidence_digest'], 'catalog_version': row['catalog_version'], 'operator': operator})
            return self.candidate(candidate_id)

    # ---------- run views ----------

    def run_view(self, run_id):
        count(run_id, 'run_id', 1)
        row = self.store.one('SELECT * FROM harvest_runs WHERE run_id=?', (run_id,))
        if row is None:
            reject('unknown_harvest_run', 'Harvest run does not exist', run_id=run_id)
        row['summary'] = json.loads(row['summary'])
        return row
