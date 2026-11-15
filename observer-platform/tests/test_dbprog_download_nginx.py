"""Downloads select streaming locations without widening other proxy routes."""
from pathlib import Path
import re
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD_PREFIXES = ('/dbprog/folder-packages/', '/dbprog/packages/', '/dbprog/objects/')


def tls_locations():
    text = (ROOT / 'deploy/nginx/https.conf.template').read_text()
    tls = text.split('listen 443 ssl;', 1)[1]
    rows = []
    for match in re.finditer(r'location\s+(?:(=|\^~)\s+)?([^\s{]+)\s*\{([^{}]*)\}', tls):
        modifier, path, body = match.groups()
        rows.append((modifier, path, body))
    # This template uses only exact and prefix locations. Do not silently
    # misrepresent routing if a future change introduces regex locations.
    assert not re.search(r'location\s+~', tls)
    return rows


def selected_location(target):
    path = urlsplit(target).path
    locations = tls_locations()
    exact = next((row for row in locations if row[0] == '=' and row[1] == path), None)
    if exact:
        return exact
    return max((row for row in locations if row[0] != '=' and path.startswith(row[1])),
               key=lambda row: len(row[1]))


@pytest.mark.parametrize('prefix', DOWNLOAD_PREFIXES)
@pytest.mark.parametrize('suffix', ('dataset-id', 'dataset-id/recording-id', 'dataset-id?path=a%2Fb&taskId=abc'))
def test_download_routes_stream_through_the_authenticated_backend(prefix, suffix):
    modifier, selected, body = selected_location(prefix + suffix)
    assert (modifier, selected) == ('^~', prefix)
    for directive in ('proxy_pass http://backend:8888;', 'proxy_http_version 1.1;',
                      'proxy_set_header Connection "";', 'proxy_set_header Host $host;',
                      'proxy_set_header X-Forwarded-Proto https;',
                      'proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;',
                      'proxy_buffering off;', 'proxy_read_timeout 300s;',
                      'proxy_send_timeout 300s;', 'send_timeout 300s;'):
        assert directive in body
    # No URI replacement, direct storage exposure, or cookie/Range override:
    # the existing Observer and warehouse checks still execute in backend.
    assert re.findall(r'proxy_pass\s+([^;]+);', body) == ['http://backend:8888']
    assert not re.search(r'\b(?:alias|root|rewrite|return|auth_request|proxy_pass_request_headers)\b', body)
    assert not re.search(r'proxy_set_header\s+(?:Cookie|Authorization|Range|If-Range)\b', body, re.I)


@pytest.mark.parametrize('target,expected,read_timeout', (
    ('/dbprog/api/v1/datasets/id/folder-package?async=true', '/dbprog/api/', '180s'),
    ('/dbprog/api/v1/files/id/download', '/dbprog/api/', '180s'),
    ('/native-camera/camera-front/video.mp4', '/native-camera/', '45s'),
    ('/dbprog/assets/index.js', '/', '30s'),
    ('/dbprog/folder-packages', '/', '30s'),
    ('/dbprog/folder-packages-other/id', '/', '30s'),
    ('/dbprog/packages-other/id', '/', '30s'),
    ('/dbprog/objects-other/id', '/', '30s'),
    ('/dbprog/other/folder-packages/id', '/', '30s'),
))
def test_other_routes_keep_their_existing_limits(target, expected, read_timeout):
    _, selected, body = selected_location(target)
    assert selected == expected
    assert 'proxy_read_timeout ' + read_timeout + ';' in body
    assert 'send_timeout 300s;' not in body
