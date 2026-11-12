"""获批目录入口的采集与审查演练。

回环来源只产生输入；本脚本用真实 HTTP 传输走完整业务流程：
登记获批入口 -> 预算内采集（预算不足时报告未完成内容）-> 补采 ->
审查候选字段证据（页面/节点/采集摘要）-> 冲突人工裁决 -> 确认绑定证据组与目录版本 ->
应用到现有资产与授权模块。远端删页与越界重定向不影响本地已有授权。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from source import start
from rights_graph.primitives import GraphError
from rights_graph.service import RightsService

server, base = start()
try:
    app = RightsService(':memory:')
    dispatch = app.dispatch

    # 1. 登记获批入口：origin 与路径前缀同时约束入口与重定向；预算故意调小。
    dispatch('harvest.source.register', {
        'source_id': 'partner', 'entry_url': base + '/approved/catalog',
        'origin': base, 'path_prefix': '/approved/', 'max_documents': 1, 'max_bytes': 65536})
    first = dispatch('harvest.run', {'source_id': 'partner'})
    assert first['state'] == 'incomplete' and first['summary']['exhausted'] == 'documents', first
    pending = first['summary']['pending']
    assert base + '/approved/license/a' in pending, pending

    # 2. 资料员看到未完成内容后提高预算补采；候选授权页取齐。
    dispatch('harvest.source.update', {'source_id': 'partner', 'max_documents': 10})
    second = dispatch('harvest.run', {'source_id': 'partner'})
    assert second['state'] == 'complete', second
    candidate = dispatch('harvest.candidates', {'source_id': 'partner'})[0]
    view = dispatch('harvest.candidate.get', {'candidate_id': candidate['candidate_id']})
    assert view['complete'] and view['state'] == 'pending', view['missing']
    purposes = view['fields']['grant']['purposes']['sources'][0]
    assert purposes['url'] == base + '/approved/license/a' and purposes['run_id'] == 2, purposes

    # 3. 审查人按当前证据摘要确认，绑定整组证据与目录版本；随后应用到现有模块。
    stale = dispatch('harvest.candidate.confirm', {
        'candidate_id': view['candidate_id'], 'reviewer': 'reviewer-li', 'reason': '证据齐全',
        'expected_evidence_digest': view['evidence_digest'], 'expected_catalog_version': view['catalog_version']})
    assert stale['state'] == 'confirmed'
    applied = dispatch('harvest.candidate.apply', {'candidate_id': view['candidate_id'], 'operator': 'librarian-wang'})
    assert applied['applied'] == {'asset_id': 'remote-a', 'grant_id': 'remote-a-grant', 'confirmation': 1}, applied
    grant = dispatch('grant.get', {'grant_id': 'remote-a-grant'})
    assert grant['policy']['max_depth'] == 3 and grant['policy']['allow_derivatives'] is False
    plan = dispatch('plan.create', {'target': 'remote-a', 'context': {
        'purpose': 'research', 'region': 'CN', 'right': 'read',
        'at': '2026-11-11T00:00:00Z', 'fulfilled_obligations': ['attribution']}})
    assert plan['target'] == 'remote-a'

    # 4. 同 ID 异文：冲突不被最后一页覆盖，审查人裁决后才能确认；应用不覆盖已有登记。
    dispatch('harvest.source.register', {
        'source_id': 'partner-conflict', 'entry_url': base + '/approved/portfolio',
        'origin': base, 'path_prefix': '/approved/', 'max_documents': 10, 'max_bytes': 65536})
    dispatch('harvest.run', {'source_id': 'partner-conflict'})
    conflicted = dispatch('harvest.candidates', {'source_id': 'partner-conflict'})[0]
    view = dispatch('harvest.candidate.get', {'candidate_id': conflicted['candidate_id']})
    titles = next(item for item in view['conflicts'] if item['field'] == 'asset.title')
    assert len(titles['values']) == 2 and not view['complete']
    resolved = dispatch('harvest.candidate.resolve', {
        'candidate_id': view['candidate_id'], 'field': 'asset.title', 'value': '合成观测集',
        'reviewer': 'reviewer-li', 'reason': '以目录页为准'})
    assert resolved['complete']
    confirmed = dispatch('harvest.candidate.confirm', {
        'candidate_id': view['candidate_id'], 'reviewer': 'reviewer-li', 'reason': '冲突已裁决',
        'expected_evidence_digest': resolved['evidence_digest'], 'expected_catalog_version': resolved['catalog_version']})
    try:
        dispatch('harvest.candidate.apply', {'candidate_id': confirmed['candidate_id'], 'operator': 'librarian-wang'})
        raise AssertionError('采集结果不应覆盖已登记资产')
    except GraphError as exc:
        assert exc.code == 'asset_exists', exc.code
    assert dispatch('asset.get', {'asset_id': 'remote-a'})['description'] == '合成观测集'

    # 5. 越界重定向被拦截；远端删页只影响候选证据，不撤销本地已有授权。
    dispatch('harvest.source.register', {
        'source_id': 'partner-redirect', 'entry_url': base + '/approved/redirect',
        'origin': base, 'path_prefix': '/approved/', 'max_documents': 5, 'max_bytes': 65536})
    redirected = dispatch('harvest.run', {'source_id': 'partner-redirect'})
    assert redirected['summary']['documents'][0]['status'] == 'blocked', redirected['summary']
    dispatch('harvest.source.register', {
        'source_id': 'partner-gone', 'entry_url': base + '/approved/license/gone',
        'origin': base, 'path_prefix': '/approved/', 'max_documents': 5, 'max_bytes': 65536})
    gone = dispatch('harvest.run', {'source_id': 'partner-gone'})
    assert gone['summary']['documents'][0]['status'] == 'missing', gone['summary']
    assert dispatch('grant.get', {'grant_id': 'remote-a-grant'})['state'] == 'active'

    assert dispatch('audit.verify', {})['ok']
    assert dispatch('system.check', {})['ok']
    print('获批目录采集与审查演练通过：补采、冲突裁决、证据绑定确认、应用与远端故障语义均符合预期')
finally:
    server.shutdown()
    server.server_close()
