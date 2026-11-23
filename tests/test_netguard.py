"""Admin-port client allow-list (system/netguard.py, app.make_tagged_app)."""
import pytest

import app as appmod
from system.netguard import client_allowed, parse_allow_nets

PRIVATE = parse_allow_nets('private')


@pytest.mark.parametrize('addr', [
    '127.0.0.1', '192.168.9.74', '10.1.2.3', '172.20.0.5', '100.76.4.11', '100.101.102.103',
    '::1', 'fd7a:115c:a1e0::1', 'fe80::1%wlan0', '::ffff:192.168.9.20',
])
def test_private_and_tailscale_clients_allowed(addr):
    assert client_allowed(addr, PRIVATE)


@pytest.mark.parametrize('addr', ['8.8.8.8', '172.32.0.1', '100.128.0.1', '2001:db8::1', '::ffff:8.8.8.8', '', 'junk'])
def test_public_or_bad_clients_refused(addr):
    assert not client_allowed(addr, PRIVATE)


def test_any_disables_the_check():
    assert parse_allow_nets('any') is None


def test_extra_cidr_added_to_private():
    nets = parse_allow_nets('private, 203.0.113.0/24')
    assert client_allowed('203.0.113.9', nets) and client_allowed('192.168.1.1', nets)


@pytest.mark.parametrize('spec', ['', ' , ', 'not-a-net', '300.1.1.0/24'])
def test_bad_spec_fails_loudly(spec):
    with pytest.raises(ValueError):
        parse_allow_nets(spec)


def _call(port, remote):
    out = {}
    def start_response(status, headers):
        out['status'] = status
    environ = {'REQUEST_METHOD': 'GET', 'PATH_INFO': '/api/mode', 'SERVER_NAME': 'x', 'SERVER_PORT': str(port),
               'wsgi.url_scheme': 'http', 'REMOTE_ADDR': remote, 'wsgi.input': __import__('io').BytesIO()}
    body = b''.join(appmod.make_tagged_app(port)(environ, start_response))
    return out['status'], body


def test_admin_port_refuses_public_client_before_flask():
    status, body = _call(appmod.ADMIN_PORT, '8.8.8.8')
    assert status.startswith('403') and b'local network or Tailscale' in body


def test_admin_port_lets_lan_client_through():
    status, _ = _call(appmod.ADMIN_PORT, '192.168.9.20')
    assert not status.startswith('403')


def test_readonly_port_is_never_restricted():
    status, _ = _call(appmod.READONLY_PORT, '8.8.8.8')
    assert status.startswith('200')


def test_admin_allow_nets_any(monkeypatch):
    monkeypatch.setattr(appmod, 'ADMIN_ALLOW_NETS', 'any')
    status, _ = _call(appmod.ADMIN_PORT, '8.8.8.8')
    assert not status.startswith('403')
