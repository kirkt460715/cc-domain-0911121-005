import urllib.error
import urllib.request
from urllib.parse import urlsplit
from rights_graph.primitives import reject

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
JSON_TYPES = ('application/json', 'application/ld+json')


def normalize_origin(origin):
    if not isinstance(origin, str):
        reject('harvest_origin', 'Origin must be text')
    parts = urlsplit(origin)
    if parts.scheme not in ('http', 'https') or not parts.netloc or '@' in parts.netloc:
        reject('harvest_origin', 'Origin must be an absolute http(s) origin without userinfo')
    if parts.path not in ('', '/') or parts.query or parts.fragment:
        reject('harvest_origin', 'Origin must not contain path, query or fragment')
    return parts.scheme + '://' + parts.netloc.lower()


def normalize_prefix(path_prefix):
    if not isinstance(path_prefix, str) or not path_prefix.startswith('/') or '..' in path_prefix.split('/'):
        reject('harvest_prefix', 'Path prefix must be absolute without parent segments')
    return path_prefix


def in_scope(origin, path_prefix, url):
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        return False
    if parts.scheme + '://' + parts.netloc.lower() != origin:
        return False
    path = parts.path or '/'
    if path_prefix.endswith('/'):
        return path.startswith(path_prefix)
    return path == path_prefix or path.startswith(path_prefix + '/')


def json_content(headers):
    content_type = headers.get('content-type', '').split(';')[0].strip().lower()
    return content_type in JSON_TYPES or content_type.endswith('+json')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def http_get(url, byte_cap, timeout=10):
    """Fetch one URL without following redirects; body is capped at byte_cap.

    Redirect responses are returned to the caller so the harvest scope rules
    decide whether the target stays inside the approved origin and prefix.
    """
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, headers={'Accept': 'application/ld+json, application/json', 'User-Agent': 'rights-graph-harvest/1'})
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(byte_cap + 1)
            truncated = len(body) > byte_cap
            headers = {key.lower(): value for key, value in response.headers.items()}
            return {'status': response.status, 'headers': headers, 'body': body[:byte_cap], 'truncated': truncated, 'error': None}
    except urllib.error.HTTPError as exc:
        headers = {key.lower(): value for key, value in (exc.headers.items() if exc.headers else [])}
        try:
            body = exc.read(byte_cap + 1)[:byte_cap] if exc.fp else b''
        except OSError:
            body = b''
        return {'status': exc.code, 'headers': headers, 'body': body, 'truncated': False, 'error': None}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {'status': None, 'headers': {}, 'body': b'', 'truncated': False, 'error': str(exc)[:200]}
