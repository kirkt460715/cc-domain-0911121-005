import copy
from tests.harvest.support import ENTRY, ORIGIN, CATALOG, LICENSE, HarvestCase, pages, transport
from rights_graph.catalog import assets
from rights_graph.policy import grants
from rights_graph.planner import plans


class HarvestFlowTests(HarvestCase):
    def test_collect_confirm_apply_produces_usable_grant(self):
        calls = []
        view = self.only_candidate(calls=calls)
        self.assertTrue(view['complete'])
        self.assertEqual(view['state'], 'pending')
        self.assertEqual(sorted(set(calls)), sorted(calls))
        asset_id = view['fields']['asset']['asset_id']['value']
        self.assertEqual(asset_id, 'remote-a')
        sources = view['fields']['grant']['purposes']['sources']
        self.assertEqual([item['url'] for item in sources], [ORIGIN + '/approved/license/a'])
        self.assertEqual([item['node'] for item in sources], [ORIGIN + '/approved/license/a'])
        self.assertEqual(sources[0]['run_id'], 1)
        self.assertEqual(len(sources[0]['sha256']), 64)
        self.assertIn('1', view['runs'])
        self.assertEqual(view['runs']['1']['state'], 'complete')
        title_sources = {item['url'] for item in view['fields']['asset']['title']['sources']}
        self.assertEqual(title_sources, {ENTRY, ORIGIN + '/approved/asset/a'})
        confirmed = self.confirm(view)
        self.assertEqual(confirmed['state'], 'confirmed')
        self.assertEqual(confirmed['confirmations'][0]['evidence_digest'], view['evidence_digest'])
        self.assertEqual(confirmed['confirmations'][0]['catalog_version'], view['catalog_version'])
        applied = self.app.harvest.apply(view['candidate_id'], 'librarian-wang')
        self.assertEqual(applied['state'], 'applied')
        self.assertEqual(applied['applied'], {'asset_id': 'remote-a', 'grant_id': 'remote-a-grant', 'confirmation': 1})
        asset = assets.get(self.store, 'remote-a')
        self.assertEqual(asset['metadata']['harvest']['node_id'], ORIGIN + '/approved/asset/a')
        self.assertEqual(asset['metadata']['harvest']['evidence_digest'], view['evidence_digest'])
        grant = grants.get(self.store, 'remote-a-grant')
        self.assertEqual(grant['policy']['max_depth'], 3)
        self.assertFalse(grant['policy']['allow_derivatives'])
        self.assertEqual(grant['policy']['valid_until'], '2027-11-01T00:00:00.000000Z')
        plan = plans.create(self.store, 'remote-a', {'purpose': 'research', 'region': 'CN', 'right': 'read', 'at': '2026-11-11T00:00:00Z', 'fulfilled_obligations': ['attribution']})
        self.assertEqual(plan['target'], 'remote-a')
        self.assertTrue(self.app.dispatch('system.check', {})['ok'])

    def test_missing_grant_page_blocks_confirmation(self):
        served = pages()
        del served[ORIGIN + '/approved/license/a']
        view = self.only_candidate(served)
        self.assertFalse(view['complete'])
        self.assertEqual(view['missing'], [{'field': 'grant', 'reason': 'evidence_not_collected', 'url': ORIGIN + '/approved/license/a', 'document': 'missing'}])
        self.fails('harvest_incomplete', self.confirm, view)
        self.fails('harvest_not_confirmed', self.app.harvest.apply, view['candidate_id'], 'librarian-wang')
        self.assertEqual(assets.listing(self.store), [])

    def test_budget_exhaustion_reports_pending_then_recollection_completes(self):
        self.register(max_documents=1)
        first = self.collect()
        self.assertEqual(first['state'], 'incomplete')
        self.assertEqual(first['summary']['exhausted'], 'documents')
        self.assertEqual(sorted(first['summary']['pending']), sorted([ORIGIN + '/approved/license/a', ORIGIN + '/approved/distribution/a']))
        view = self.only_candidate()
        self.assertFalse(view['complete'])
        self.assertEqual(view['missing'][0]['document'], 'pending')
        self.app.harvest.update('partner', max_documents=10)
        second = self.collect()
        self.assertEqual(second['state'], 'complete')
        view = self.app.harvest.candidate(view['candidate_id'])
        self.assertTrue(view['complete'])
        self.assertEqual(view['state'], 'pending')

    def test_bytes_budget_exhaustion_reports_pending(self):
        self.register(max_bytes=64)
        run = self.collect()
        self.assertEqual(run['state'], 'incomplete')
        self.assertEqual(run['summary']['exhausted'], 'bytes')
        self.assertEqual(run['summary']['pending'], [ENTRY])
        self.assertEqual(self.app.harvest.candidates('partner'), [])

    def test_conflict_is_recorded_not_overwritten_and_requires_resolution(self):
        served = pages()
        catalog = copy.deepcopy(CATALOG)
        catalog['@graph'][0]['seeAlso'] = {'@id': '/approved/conflict'}
        served[ENTRY]['json'] = catalog
        served[ORIGIN + '/approved/conflict'] = {'json': {'@id': '/approved/asset/a', '@type': 'Asset', 'title': '冲突的目录标题'}}
        view = self.only_candidate(served)
        self.assertFalse(view['complete'])
        conflict = next(item for item in view['conflicts'] if item['field'] == 'asset.title')
        titles = sorted(item['value'] for item in conflict['values'])
        self.assertEqual(titles, ['冲突的目录标题', '合成观测集'])
        self.assertIn('seeAlso', view['fields']['unmapped']['asset'])
        self.fails('harvest_incomplete', self.confirm, view)
        self.fails('harvest_resolution_value', self.app.harvest.resolve, view['candidate_id'], 'asset.title', '编造标题', 'reviewer-li', '选择页面值')
        self.fails('harvest_resolution_inapplicable', self.app.harvest.resolve, view['candidate_id'], 'asset.owner', 'other-owner', 'reviewer-li', '覆盖远端值')
        resolved = self.app.harvest.resolve(view['candidate_id'], 'asset.title', '合成观测集', 'reviewer-li', '以目录页为准')
        self.assertTrue(resolved['complete'])
        self.assertEqual(resolved['fields']['asset']['title']['origin'], 'manual')
        self.assertEqual(resolved['fields']['asset']['title']['manual']['reviewer'], 'reviewer-li')
        confirmed = self.confirm(resolved)
        applied = self.app.harvest.apply(confirmed['candidate_id'], 'librarian-wang')
        self.assertEqual(assets.get(self.store, applied['applied']['asset_id'])['description'], '合成观测集')

    def test_page_change_after_confirmation_requires_new_review(self):
        served = pages()
        view = self.only_candidate(served)
        confirmed = self.confirm(view)
        served[ORIGIN + '/approved/license/a'] = {'json': {**LICENSE, 'purposes': ['research', 'search']}}
        self.collect(served)
        changed = self.app.harvest.candidate(view['candidate_id'])
        self.assertEqual(changed['state'], 'pending')
        self.assertTrue(changed['changed_since_confirmation'])
        self.assertNotEqual(changed['evidence_digest'], view['evidence_digest'])
        self.fails('harvest_not_confirmed', self.app.harvest.apply, view['candidate_id'], 'librarian-wang')
        self.fails('harvest_evidence_stale', self.confirm, view)
        renewed = self.confirm(changed)
        applied = self.app.harvest.apply(renewed['candidate_id'], 'librarian-wang')
        self.assertEqual(applied['state'], 'applied')
        self.assertEqual(grants.get(self.store, 'remote-a-grant')['policy']['purposes'], ['research', 'search'])

    def test_remote_deletion_does_not_revoke_local_grant(self):
        served = pages()
        view = self.only_candidate(served)
        applied = self.app.harvest.apply(self.confirm(view)['candidate_id'], 'librarian-wang')
        self.assertEqual(applied['state'], 'applied')
        served[ORIGIN + '/approved/license/a'] = {'status': 404}
        self.collect(served)
        current = self.app.harvest.candidate(view['candidate_id'])
        self.assertEqual(current['state'], 'applied')
        self.assertTrue(current['changed_since_apply'])
        self.assertFalse(current['complete'])
        self.assertEqual(current['missing'][0]['document'], 'missing')
        grant = grants.get(self.store, 'remote-a-grant')
        self.assertEqual(grant['state'], 'active')
        self.assertEqual(grant['revision'], 1)

    def test_manual_completion_records_human_value_source(self):
        served = pages()
        reduced = {key: value for key, value in LICENSE.items() if key not in ('max_depth', 'allow_derivatives')}
        served[ORIGIN + '/approved/license/a'] = {'json': reduced}
        view = self.only_candidate(served)
        self.assertFalse(view['complete'])
        self.assertEqual(sorted(item['field'] for item in view['missing']), ['grant.allow_derivatives', 'grant.max_depth'])
        self.fails('harvest_incomplete', self.confirm, view)
        self.app.harvest.resolve(view['candidate_id'], 'grant.max_depth', 1, 'reviewer-li', '合作方口头确认深度')
        resolved = self.app.harvest.resolve(view['candidate_id'], 'grant.allow_derivatives', False, 'reviewer-li', '合作方口头确认不允许衍生')
        self.assertTrue(resolved['complete'])
        self.assertEqual(resolved['fields']['grant']['max_depth']['origin'], 'manual')
        applied = self.app.harvest.apply(self.confirm(resolved)['candidate_id'], 'librarian-wang')
        grant = grants.get(self.store, applied['applied']['grant_id'])
        self.assertEqual(grant['policy']['max_depth'], 1)
        self.assertFalse(grant['policy']['allow_derivatives'])

    def test_collection_never_overwrites_existing_catalog(self):
        self.source('remote-a', grant_id='remote-a-grant')
        before = grants.get(self.store, 'remote-a-grant')
        view = self.only_candidate()
        confirmed = self.confirm(view)
        self.fails('asset_exists', self.app.harvest.apply, confirmed['candidate_id'], 'librarian-wang')
        self.assertEqual(self.app.harvest.candidate(view['candidate_id'])['state'], 'confirmed')
        self.assertEqual(grants.get(self.store, 'remote-a-grant'), before)
        self.assertEqual(assets.get(self.store, 'remote-a')['description'], 'Synthetic source')

    def test_unchanged_recollection_preserves_confirmation(self):
        served = pages()
        view = self.only_candidate(served)
        confirmed = self.confirm(view)
        second = self.collect(served)
        self.assertEqual(second['state'], 'complete')
        current = self.app.harvest.candidate(view['candidate_id'])
        self.assertEqual(current['state'], 'confirmed')
        self.assertEqual(current['evidence_digest'], confirmed['evidence_digest'])
        self.assertEqual(current['catalog_version'], confirmed['catalog_version'])
        self.assertFalse(current['changed_since_confirmation'])
        self.assertEqual(second['summary']['candidates'][0]['candidate_id'], view['candidate_id'])
        applied = self.app.harvest.apply(view['candidate_id'], 'librarian-wang')
        self.assertEqual(applied['state'], 'applied')

    def test_reopen_reject_and_evidence_driven_re_review(self):
        served = pages()
        view = self.only_candidate(served)
        confirmed = self.confirm(view)
        self.fails('harvest_candidate_state', self.app.harvest.resolve, view['candidate_id'], 'asset.title', 'x', 'reviewer-li', '已确认不可改')
        reopened = self.app.harvest.reopen(view['candidate_id'], 'reviewer-li', '需要补充说明')
        self.assertEqual(reopened['state'], 'pending')
        self.assertEqual(confirmed['confirmations'][0]['ordinal'], 1)
        rejected = self.app.harvest.reject_candidate(view['candidate_id'], 'reviewer-li', '暂不引入')
        self.assertEqual(rejected['state'], 'rejected')
        served[ENTRY]['json']['@graph'][0]['title'] = '更新的目录标题'
        self.collect(served)
        self.assertEqual(self.app.harvest.candidate(view['candidate_id'])['state'], 'pending')


class HarvestScopeTests(HarvestCase):
    def test_entry_must_stay_inside_approved_scope(self):
        self.fails('harvest_entry_scope', self.register, entry='http://other.test/approved/catalog')
        self.fails('harvest_entry_scope', self.register, entry=ORIGIN + '/outside/catalog')
        self.fails('harvest_origin', self.register, entry=ENTRY, origin='ftp://partner.test')
        self.fails('harvest_prefix', self.register, prefix='/approved/../etc')
        self.register()
        self.fails('harvest_source_exists', self.register)
        self.assertEqual(self.app.harvest.source('partner')['state'], 'active')

    def test_redirect_outside_scope_is_blocked(self):
        self.register(entry=ORIGIN + '/approved/redirect')
        run = self.collect({ORIGIN + '/approved/redirect': {'location': '/outside'}})
        self.assertEqual(run['state'], 'complete')
        document = run['summary']['documents'][0]
        self.assertEqual(document['status'], 'blocked')
        self.assertEqual(document['detail'], 'redirect_out_of_scope')
        self.assertEqual(self.app.harvest.candidates('partner'), [])

    def test_redirect_inside_scope_is_followed(self):
        self.register(entry=ORIGIN + '/approved/alias')
        served = pages()
        served[ORIGIN + '/approved/alias'] = {'location': '/approved/catalog'}
        run = self.collect(served)
        self.assertEqual(run['state'], 'complete')
        document = next(item for item in run['summary']['documents'] if item['url'] == ORIGIN + '/approved/alias')
        self.assertEqual(document['status'], 'fetched')
        self.assertEqual(document['final_url'], ENTRY)
        self.assertTrue(self.only_candidate(served)['complete'])

    def test_redirect_loop_is_an_error_document(self):
        self.register(entry=ORIGIN + '/approved/loop')
        run = self.collect({ORIGIN + '/approved/loop': {'location': '/approved/loop'}})
        self.assertEqual(run['summary']['documents'][0]['detail'], 'redirect_loop')

    def test_suspended_source_cannot_run(self):
        self.register()
        self.app.harvest.update('partner', state='suspended')
        self.fails('harvest_source_state', self.collect)
        self.app.harvest.update('partner', state='active')
        self.assertEqual(self.collect()['state'], 'complete')


class HarvestProtocolTests(HarvestCase):
    def test_blank_nodes_do_not_leak_across_documents(self):
        served = pages()
        license_page = copy.deepcopy(LICENSE)
        license_page['note'] = {'@id': '_:note'}
        served[ORIGIN + '/approved/license/a'] = {'json': license_page}
        calls = []
        view = self.only_candidate(served, calls=calls)
        self.assertTrue(view['complete'])
        self.assertNotIn('_:note', [item for page in served for item in []])
        self.assertFalse(any('_:' in url for url in calls))
        run = self.app.harvest.source('partner')['latest_run']
        self.assertEqual(run['summary']['blank_nodes'], 2)

    def test_unknown_vocabulary_is_visible_but_not_inferred(self):
        served = pages()
        catalog = copy.deepcopy(CATALOG)
        catalog['@context'] = 'http://partner.test/context.jsonld'
        catalog['@graph'][0]['signature'] = 'unmapped-value'
        catalog['@graph'].append({'@id': '/approved/contract/x', '@type': 'Contract', 'clause': '文本'})
        served[ENTRY]['json'] = catalog
        calls = []
        view = self.only_candidate(served, calls=calls)
        self.assertTrue(view['complete'])
        self.assertNotIn(ORIGIN + '/context.jsonld', calls)
        self.assertIn('signature', view['fields']['unmapped']['asset'])
        run = self.app.harvest.source('partner')['latest_run']
        self.assertEqual(run['summary']['unknown_types'], ['Contract'])
        self.assertEqual(len(self.app.harvest.candidates('partner')), 1)

    def test_cycles_terminate_and_each_page_fetched_once(self):
        calls = []
        self.only_candidate(calls=calls)
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(set(calls), {ENTRY, ORIGIN + '/approved/asset/a', ORIGIN + '/approved/license/a', ORIGIN + '/approved/distribution/a'})

    def test_out_of_scope_links_are_reported_not_followed(self):
        served = pages()
        catalog = copy.deepcopy(CATALOG)
        catalog['@graph'][0]['mirror'] = {'@id': 'http://mirror.test/approved/asset/a'}
        served[ENTRY]['json'] = catalog
        calls = []
        view = self.only_candidate(served, calls=calls)
        self.assertTrue(view['complete'])
        self.assertFalse(any('mirror.test' in url for url in calls))
        run = self.app.harvest.source('partner')['latest_run']
        self.assertEqual(run['summary']['out_of_scope'], [{'url': 'http://mirror.test/approved/asset/a', 'from': ENTRY}])

    def test_dispatch_and_system_check_cover_harvest(self):
        self.app.dispatch('harvest.source.register', {'source_id': 'desk', 'entry_url': ENTRY, 'origin': ORIGIN, 'path_prefix': '/approved/', 'max_documents': 5, 'max_bytes': 65536})
        listed = self.app.dispatch('harvest.source.list', {})
        self.assertEqual([row['source_id'] for row in listed], ['desk'])
        self.app.harvest.run('desk', transport(pages()))
        candidate = self.app.dispatch('harvest.candidates', {'source_id': 'desk'})[0]
        view = self.app.dispatch('harvest.candidate.get', {'candidate_id': candidate['candidate_id']})
        self.assertTrue(view['complete'])
        self.assertTrue(self.app.dispatch('system.check', {})['ok'])
        self.store.execute("UPDATE harvest_candidates SET evidence='{}' WHERE candidate_id=?", (candidate['candidate_id'],))
        check = self.app.dispatch('system.check', {})
        self.assertFalse(check['ok'])
        self.assertIn('harvest_evidence_digest', {problem['code'] for problem in check['problems']})
