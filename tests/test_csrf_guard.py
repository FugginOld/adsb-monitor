"""Cross-site write guard (system/auth.py csrf_guard) and the range-unit fix."""
import pytest
from flask.testing import FlaskClient

import app as appmod


@pytest.fixture
def raw():
    """A client WITHOUT the dashboard's X-Requested-With header."""
    appmod.app.config['TESTING'] = True
    return FlaskClient(appmod.app, appmod.app.response_class, use_cookies=True)


def test_post_without_header_is_refused(raw, fake_host):
    r = raw.post('/api/service/readsb/restart')
    assert r.status_code == 403
    assert 'X-Requested-With' in r.get_json()['error']
    assert not any(c[0] == 'run' for c in fake_host.calls)


def test_form_style_multipart_post_is_refused(raw):
    r = raw.post('/api/versions/refresh', data={'x': '1'})
    assert r.status_code == 403


@pytest.mark.parametrize('site', ['cross-site', 'same-site'])
def test_cross_site_post_is_refused_even_with_header(raw, site):
    r = raw.post('/api/versions/refresh',
                 headers={'X-Requested-With': 'adsb-monitor', 'Sec-Fetch-Site': site})
    assert r.status_code == 403
    assert 'Cross-site' in r.get_json()['error']


def test_wrong_header_value_is_refused(raw):
    r = raw.post('/api/versions/refresh', headers={'X-Requested-With': 'XMLHttpRequest'})
    assert r.status_code == 403


@pytest.mark.parametrize('site', [None, 'same-origin', 'none'])
def test_same_origin_post_with_header_passes_the_guard(raw, site, monkeypatch):
    import routes.versions as rv
    monkeypatch.setattr(rv, 'refresh_versions', lambda *a, **k: None)
    headers = {'X-Requested-With': 'adsb-monitor'}
    if site:
        headers['Sec-Fetch-Site'] = site
    r = raw.post('/api/versions/refresh', headers=headers)
    assert r.status_code != 403


def test_get_needs_no_header(raw):
    assert raw.get('/api/mode').status_code == 200


def test_readsb_max_range_is_r_dst_in_nautical_miles(fake_host):
    fake_host.files = {
        appmod.READSB_JSON + '/aircraft.json': {'aircraft': [
            {'seen': 1, 'r_dst': 186.4}, {'seen': 2, 'r_dst': 40.0},
            {'seen': 120, 'r_dst': 300.0},  # stale, ignored
        ]},
    }
    assert appmod.readsb_metrics()['max_range_nm'] == 186
