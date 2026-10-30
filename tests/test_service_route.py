import pytest

import app as appmod
from fakes import Result
import routes.services as svc_route


@pytest.fixture
def client(monkeypatch):
    appmod.app.config['TESTING'] = True
    monkeypatch.setattr(svc_route, 'load_config', lambda: [{'key': 'readsb'}])
    return appmod.app.test_client()


def test_restart_configured_service(client, fake_host):
    fake_host.commands = {('systemctl', 'restart', 'readsb'): Result(0, 'ok', '')}
    body = client.post('/api/service/readsb/restart').get_json()
    assert body['ok'] is True
    assert ('run', ('systemctl', 'restart', 'readsb')) in fake_host.calls


@pytest.mark.parametrize('name', ['dbus', 'readsb.service', 'a;b', '-x'])
def test_restart_rejects_unconfigured_name(client, fake_host, name):
    body = client.post(f'/api/service/{name}/restart').get_json()
    assert body['ok'] is False
    assert not [c for c in fake_host.calls if c[0] == 'run']


def test_feeder_cfg_restart_uses_configured_key(monkeypatch, fake_host):
    import routes.settings as settings_route
    appmod.app.config['TESTING'] = True
    monkeypatch.setattr(settings_route, 'set_feeder_settings', lambda key, data: (True, 'ok'))
    monkeypatch.setattr(settings_route, 'load_config', lambda: [{'key': 'readsb', 'kind': 'service'}])
    fake_host.commands = {('systemctl', 'restart', 'readsb'): Result(0, 'ok', '')}
    c = appmod.app.test_client()
    c.post('/api/settings/feeder/readsb', json={})
    assert ('run', ('systemctl', 'restart', 'readsb')) in fake_host.calls
    fake_host.calls.clear()
    c.post('/api/settings/feeder/dbus', json={})
    assert not [x for x in fake_host.calls if x[0] == 'run']
