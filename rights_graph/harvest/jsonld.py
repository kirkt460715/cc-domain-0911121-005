from urllib.parse import urljoin
from rights_graph.primitives import json_loads, reject

KNOWN_TYPES = ('Asset', 'Distribution', 'Grant')


def parse_document(text, page_url):
    """Parse one JSON-LD page under the approved drill protocol.

    Only objects, arrays, @graph, absolute or relative @id and explicit @type
    are interpreted. Remote contexts are never fetched and never applied, and
    unknown vocabulary is recorded but never inferred. Blank node identifiers
    are valid inside this document only and never join the shared node index.
    """
    value = json_loads(text)
    if not isinstance(value, dict):
        reject('jsonld_document', 'JSON-LD page must be a JSON object')
    result = {
        'nodes': [],
        'links': [],
        'blank_nodes': 0,
        'context_ignored': '@context' in value,
        'unknown_types': [],
    }

    def resolve(reference):
        if not isinstance(reference, str) or not reference.strip():
            reject('jsonld_id', '@id must be nonempty text')
        return urljoin(page_url, reference)

    def scan_value(item):
        if isinstance(item, dict):
            if '@id' in item or '@type' in item:
                return scan_node(item)
            return {key: scan_value(sub) for key, sub in item.items()}
        if isinstance(item, list):
            return [scan_value(sub) for sub in item]
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item
        reject('jsonld_value', 'Unsupported JSON value in page')

    def scan_node(obj):
        node_id = obj.get('@id')
        if node_id is not None and not isinstance(node_id, str):
            reject('jsonld_id', '@id must be nonempty text')
        blank = isinstance(node_id, str) and node_id.startswith('_:')
        resolved = None if node_id is None or blank else resolve(node_id)
        keys = [key for key in obj if key not in ('@id', '@type', '@context', '@graph')]
        node_type = obj.get('@type')
        if node_type is not None and not isinstance(node_type, str):
            reject('jsonld_type', '@type must be text')
        if not keys and node_type is None and '@graph' not in obj:
            if resolved is not None:
                result['links'].append(resolved)
                return {'@id': resolved}
            if blank:
                return {'@id': node_id}
            return {}
        if node_type is not None and node_type not in KNOWN_TYPES and node_type not in result['unknown_types']:
            result['unknown_types'].append(node_type)
        fields = {key: scan_value(obj[key]) for key in keys}
        graph = obj.get('@graph')
        if graph is not None:
            if not isinstance(graph, list) or any(not isinstance(item, dict) for item in graph):
                reject('jsonld_graph', '@graph must be an array of objects')
            for item in graph:
                scan_node(item)
        if blank:
            result['blank_nodes'] += 1
        if resolved is not None or node_type is not None or blank:
            result['nodes'].append({'id': resolved, 'blank': blank, 'type': node_type, 'fields': fields})
        if resolved is not None:
            return {'@id': resolved}
        if blank:
            return {'@id': node_id}
        if node_type is not None:
            return {'@type': node_type}
        return fields

    scan_node(value)
    result['links'] = sorted(set(result['links']))
    return result
