"""Front-end guards: CSP header, no inline JS, and every data-click/data-change
action used in the markup or templates has a handler in static/js/init.js."""
import glob
import os
import re

import pytest

import app as appmod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = [os.path.join(ROOT, 'static', n) for n in ('index.html', 'login.html')]
JS = sorted(glob.glob(os.path.join(ROOT, 'static', 'js', '*.js')))


def read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


@pytest.mark.parametrize('path', HTML)
def test_no_inline_scripts_or_handlers(path):
    s = read(path)
    assert not re.search(r'<script(?![^>]*\bsrc=)[^>]*>', s, re.I), 'inline <script> (blocked by the CSP)'
    assert not re.search(r'\son[a-z]+\s*=', s, re.I), 'inline on*= handler (blocked by the CSP)'
    assert 'javascript:' not in s.lower()


@pytest.mark.parametrize('path', JS)
def test_templates_have_no_inline_handlers(path):
    # handlers assigned as properties (el.onclick = fn) are fine; attributes in markup strings are not
    assert not re.search(r'<[^>`]*\son[a-z]+\s*=\s*["\']', read(path), re.I)


def _actions(kind):
    tables = read(os.path.join(ROOT, 'static', 'js', 'init.js'))
    body = re.search(rf'const {kind}_ACTIONS = \{{(.*?)\n\}};', tables, re.S).group(1)
    return set(re.findall(r'^\s{2}(\w+):', body, re.M))


@pytest.mark.parametrize('attr,kind', [('data-click', 'CLICK'), ('data-change', 'CHANGE')])
def test_every_action_used_has_a_handler(attr, kind):
    used = set()
    for p in HTML + JS:
        used |= set(re.findall(rf'{attr}="(\w+)"', read(p)))
    assert used, f'no {attr} found'
    missing = used - _actions(kind)
    assert not missing, f'{attr} without a handler in init.js: {sorted(missing)}'


def test_scripts_listed_in_index_exist_in_order():
    s = read(HTML[0])
    srcs = re.findall(r'<script src="/static/js/([\w.]+)"></script>', s)
    assert srcs == ['core.js', 'status.js', 'settings.js', 'logs.js', 'init.js']
    for n in srcs:
        assert os.path.exists(os.path.join(ROOT, 'static', 'js', n))


def test_esc_defined_once():
    defs = [p for p in JS if re.search(r'^function esc\(', read(p), re.M)]
    assert [os.path.basename(p) for p in defs] == ['core.js']


@pytest.mark.parametrize('path', ['/login', '/'])
def test_html_pages_send_the_csp(path):
    r = appmod.app.test_client().get(path)
    csp = r.headers.get('Content-Security-Policy', '')
    assert "script-src 'self'" in csp and "object-src 'none'" in csp
    assert 'unsafe-inline' not in csp.split('script-src', 1)[1].split(';', 1)[0]
    assert r.headers.get('X-Content-Type-Options') == 'nosniff'


def test_json_responses_skip_the_csp():
    r = appmod.app.test_client().get('/api/login/status')
    assert 'Content-Security-Policy' not in r.headers
