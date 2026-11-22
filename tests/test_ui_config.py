"""/api/ui-config: map URL plus tar1090's installed version for the iframe cache-bust."""
import pytest

import app as appmod


@pytest.fixture
def client():
    appmod.app.config['TESTING'] = True
    return appmod.app.test_client()


def _set_port(port):
    appmod._request_port.port = port


def test_admin_port_returns_local_url_and_version(client, fake_host, monkeypatch):
    monkeypatch.setattr(appmod, 'TAR1090_URL_LOCAL', 'http://pi:8504')
    fake_host.files = {'/usr/local/share/tar1090/git/version': '3.14.1827\n'}
    _set_port(appmod.ADMIN_PORT)
    d = client.get('/api/ui-config').get_json()
    assert d['tar1090_url'] == 'http://pi:8504'
    assert d['tar1090_version'] == '3.14.1827'


def test_readonly_port_returns_remote_url_and_version(client, fake_host, monkeypatch):
    monkeypatch.setattr(appmod, 'TAR1090_URL_REMOTE', 'https://example.org/tar1090/')
    fake_host.files = {'/usr/local/share/tar1090/version': '3.14.1827'}
    try:
        _set_port(appmod.READONLY_PORT)
        d = client.get('/api/ui-config').get_json()
    finally:
        _set_port(appmod.ADMIN_PORT)
    assert d['tar1090_url'] == 'https://example.org/tar1090/'
    assert d['tar1090_version'] == '3.14.1827'


def test_version_is_null_when_tar1090_not_installed(client):
    _set_port(appmod.ADMIN_PORT)
    assert client.get('/api/ui-config').get_json()['tar1090_version'] is None
