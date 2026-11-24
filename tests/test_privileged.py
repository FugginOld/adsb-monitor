"""The privileged gateway (system/privileged.py): what the web process may ask root for."""
import io
import json
import os
import zipfile

import pytest

import app as appmod
from app import Result
from system import privileged
from system.init_adapter import BACKUP_SUFFIX, REFUSED_MSG, LinuxHost
from system.sdr_settings import BIASTEE_1090_CONF

Refused = privileged.Refused


def go(host, verb, *args, stdin=b''):
    return privileged.dispatch(host, verb, list(args), stdin)


# ── systemctl ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize('act', ['start', 'stop', 'restart', 'try-restart', 'reset-failed'])
def test_systemctl_actions_on_a_service(fake_host, act):
    go(fake_host, 'systemctl', act, 'readsb')
    assert ('run', ('systemctl', act, 'readsb')) in fake_host.calls


def test_daemon_reload(fake_host):
    go(fake_host, 'systemctl', 'daemon-reload')
    assert ('run', ('systemctl', 'daemon-reload')) in fake_host.calls


@pytest.mark.parametrize('args', [
    ['enable', 'readsb'], ['mask', 'readsb'], ['restart'], ['start', 'debug-shell'],
    ['start', 'debug-shell.service'], ['stop', 'systemd-logind'], ['start', 'rescue'],
    ['start', 'emergency.service'], ['start', 'foo.socket'], ['start', 'foo.timer'],
    ['start', '-H'], ['start', 'a b'], ['daemon-reload', 'x'],
])
def test_systemctl_refusals(fake_host, args):
    with pytest.raises(Refused):
        go(fake_host, 'systemctl', *args)
    assert not [c for c in fake_host.calls if c[0] == 'run']


# ── write: where ────────────────────────────────────────────────────────────
def test_write_managed_file_and_its_backup(fake_host):
    go(fake_host, 'write', appmod.READSB_DEFAULT, '--backup', stdin=b'RECEIVER_OPTIONS="--net-only"\n')
    assert fake_host.files[appmod.READSB_DEFAULT] == 'RECEIVER_OPTIONS="--net-only"\n'
    bak = os.path.realpath(appmod.READSB_DEFAULT) + BACKUP_SUFFIX
    go(fake_host, 'write', bak, stdin=b'RECEIVER_OPTIONS=""\n')
    assert fake_host.files[bak] == 'RECEIVER_OPTIONS=""\n'


@pytest.mark.parametrize('path', [
    '/etc/passwd', '/etc/sudoers.d/x', '/etc/systemd/system/adsb-monitor.service',
    '/etc/default/readsb/../shadow', '/etc/default/readsb.bak', '/opt/adsb-monitor/app.py',
    '/opt/adsb-monitor/helper.py', '', 'relative/readsb',
])
def test_write_refuses_other_paths(fake_host, path):
    with pytest.raises(Refused):
        go(fake_host, 'write', path, stdin=b'X=1\n')
    assert not [c for c in fake_host.calls if c[0] == 'write_text']


def test_every_backup_file_has_a_content_kind():
    kinds = privileged.writable_paths()        # raises if a restorable file has no kind
    assert '/etc/systemd/system/adsb-monitor.service' not in kinds


# ── write: what ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize('text', [
    'GAIN=13\n', '# comment\n\nG=-g\nM=\n', 'OPTIONS="-v -f 1 -w 5 -e 20 -C 95 -E 60"\n',
    'NET="-l 47787:beast"\n',
])
def test_env_files_accept_plain_assignments(fake_host, text):
    go(fake_host, 'write', appmod.AIRSPY_DEFAULT, stdin=text.encode())


@pytest.mark.parametrize('text', [
    'GAIN=$(id)\n', 'GAIN=`id`\n', 'GAIN=13; rm -rf /\n', 'GAIN=13 && x\n', 'GAIN=13 | x\n',
    'GAIN=${PATH}\n', 'rm -rf /\n', 'export GAIN=13\n', '9X=1\n', 'GAIN=13 > /etc/x\n',
    'GAIN=a\\\nb\n', 'X=\x00\n',
])
def test_env_files_refuse_shell(fake_host, text):
    with pytest.raises(Refused):
        go(fake_host, 'write', appmod.AIRSPY_DEFAULT, stdin=text.encode())


def test_biastee_accepts_only_generated_dropins(fake_host):
    plain = '[Service]\nExecStartPre=/bin/sh -c "rtl_biast -d 0 -b 1; true"\n'
    serial = ('[Service]\nExecStartPre=/bin/sh -c "for i in 0 1 2 3; do rtl_eeprom -d $i 2>/dev/null '
              '| grep -q 00001090 && { rtl_biast -d $i -b 1; break; }; done; true"\n')
    go(fake_host, 'write', BIASTEE_1090_CONF, stdin=plain.encode())
    go(fake_host, 'write', BIASTEE_1090_CONF, stdin=serial.encode())
    for bad in (plain.replace('rtl_biast', 'id;rtl_biast'),
                serial.replace('00001090', '0; curl x|sh; #'),
                serial.replace('00001090', ''),
                '[Service]\nExecStartPre=/bin/sh -c id\n',
                plain + 'ExecStartPost=/bin/id\n'):
        with pytest.raises(Refused):
            go(fake_host, 'write', BIASTEE_1090_CONF, stdin=bad.encode())


def test_dashboard_biastee_writer_passes_the_check(fake_host):
    """The two drop-ins write_sdr1090 generates must be accepted (with and without --device)."""
    from system.sdr_settings import write_sdr1090
    for opts in ('RECEIVER_OPTIONS="--device 00001090 --gain 40"\n', 'RECEIVER_OPTIONS="--gain 40"\n'):
        fake_host.files[appmod.READSB_DEFAULT] = opts
        write_sdr1090('40', True)
        text = fake_host.files[BIASTEE_1090_CONF]
        privileged.validate_content('biastee', text.encode())


def test_uuid_ini_and_piaware_kinds():
    privileged.validate_content('uuid', b'0123abcd-0000-4000-8000-0123456789ab\n')
    with pytest.raises(Refused):
        privileged.validate_content('uuid', b'$(id)\n')
    privileged.validate_content('ini', b'[fr24]\nreceiver="beast-tcp"\nhost="127.0.0.1:30005"\n')
    with pytest.raises(Refused):
        privileged.validate_content('ini', b'host="$(id)"\n')
    privileged.validate_content('piaware', b'# conf\nreceiver-type other\nreceiver-host 127.0.0.1\n')
    with pytest.raises(Refused):
        privileged.validate_content('piaware', b'receiver-host [exec id]\n')


# ── rm / mkdir ──────────────────────────────────────────────────────────────
def test_rm_and_mkdir_only_managed(fake_host):
    fake_host.files[BIASTEE_1090_CONF] = 'x'
    go(fake_host, 'rm', BIASTEE_1090_CONF)
    assert BIASTEE_1090_CONF not in fake_host.files
    go(fake_host, 'mkdir', os.path.dirname(BIASTEE_1090_CONF))
    for verb, path in (('rm', '/etc/passwd'), ('rm', '/'), ('mkdir', '/etc/sudoers.d'), ('mkdir', '/root/.ssh')):
        with pytest.raises(Refused):
            go(fake_host, verb, path)


# ── piaware-config ──────────────────────────────────────────────────────────
def test_piaware_config_fields_and_values(fake_host):
    go(fake_host, 'piaware-config', 'receiver-host', '127.0.0.1')
    assert ('run', ('piaware-config', 'receiver-host', '127.0.0.1')) in fake_host.calls
    for args in (('allow-auto-updates', 'yes'), ('receiver-host', '[exec id]'),
                 ('receiver-host', '-x'), ('receiver-host', 'a b'), ('receiver-host', '')):
        with pytest.raises(Refused):
            go(fake_host, 'piaware-config', *args)


# ── docker ──────────────────────────────────────────────────────────────────
def test_docker_inspect_formats(fake_host):
    go(fake_host, 'docker-inspect', 'airnavradar', '{{.State.Status}}')
    assert ('run', ('docker', 'inspect', '--format', '{{.State.Status}}', 'airnavradar')) in fake_host.calls
    with pytest.raises(Refused):
        go(fake_host, 'docker-inspect', 'airnavradar', '{{json .}}')
    with pytest.raises(Refused):
        go(fake_host, 'docker-inspect', '--privileged')


def test_docker_logs_argv():
    assert privileged.docker_logs_argv('airnavradar', 200) == [
        'docker', 'logs', '-f', '--tail', '200', '--timestamps', 'airnavradar']
    with pytest.raises(Refused):
        privileged.docker_logs_argv('-H', 1)


def test_docker_recreate_takes_flags_from_the_existing_container(fake_host):
    info = [{'Config': {'Image': 'ghcr.io/x/airnav:latest'}, 'HostConfig': {'ExtraHosts': ['h:1.2.3.4']}}]
    fake_host.commands[('docker', 'inspect', 'airnavradar')] = Result(0, json.dumps(info), '')
    fake_host.default_command = Result(0, '', '')
    r = go(fake_host, 'docker-recreate', 'airnavradar', stdin=json.dumps({'BEASTHOST': '127.0.0.1'}).encode())
    assert r.ok
    run = [c[1] for c in fake_host.calls if c[0] == 'run' and c[1][:2] == ('docker', 'run')][0]
    assert run[-1] == 'ghcr.io/x/airnav:latest' and '--privileged' not in run
    assert ('-e', 'BEASTHOST=127.0.0.1') == run[run.index('-e'):run.index('-e') + 2]


@pytest.mark.parametrize('container,env', [
    ('ubuntu', {}), ('airnavradar', {'X': 'a\nb'}), ('airnavradar', {'BAD NAME': 'x'}),
    ('airnavradar', ['-v', '/:/host']), ('airnavradar', {'X': 1}),
])
def test_docker_recreate_refusals(fake_host, container, env):
    with pytest.raises(Refused):
        go(fake_host, 'docker-recreate', container, stdin=json.dumps(env).encode())


# ── stack update / graphs ───────────────────────────────────────────────────
def test_stack_update_known_component_only(fake_host):
    fake_host.default_command = Result(0, '', '')
    assert go(fake_host, 'stack-update', 'readsb').ok
    run = [c[1] for c in fake_host.calls if c[0] == 'run' and c[1][0] == 'systemd-run'][0]
    assert 'https://raw.githubusercontent.com/wiedehopf/adsb-scripts/master/readsb-install.sh' in run
    for args in ((), ('nope',), ('readsb', 'https://evil.example/x.sh')):
        with pytest.raises(Refused):
            go(fake_host, 'stack-update', *args)


def test_restore_graphs_bad_zip_is_reported_not_raised(fake_host, tmp_path, monkeypatch):
    import routes.backup as backup
    monkeypatch.setattr(backup, 'GRAPHS_RRD_DIR', str(tmp_path / 'rrd'))
    r = go(fake_host, 'restore-graphs', stdin=b'not a zip')
    assert r.code == 2 and 'BadZipFile' in r.err
    assert ('run', ('systemctl', 'start', 'collectd')) in fake_host.calls    # restarted after a failure


def test_restore_graphs_extracts(fake_host, tmp_path, monkeypatch):
    import routes.backup as backup
    monkeypatch.setattr(backup, 'GRAPHS_RRD_DIR', str(tmp_path / 'rrd'))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('localhost/dump1090-localhost/dump1090_messages-local_accepted.rrd', b'RRD')
        zf.writestr('../../escape.rrd', b'x')
    r = go(fake_host, 'restore-graphs', stdin=buf.getvalue())
    assert r.ok and r.out == '1'
    assert not (tmp_path / 'escape.rrd').exists()


def test_unknown_verb(fake_host):
    with pytest.raises(Refused):
        go(fake_host, 'sh', '-c', 'id')


# ── the sudo side ───────────────────────────────────────────────────────────
def test_unprivileged_host_goes_through_sudo(monkeypatch):
    seen = {}
    class R:
        returncode, stdout, stderr = 0, b'ok', b''
    def fake_run(argv, input=None, capture_output=None, timeout=None):
        seen.update(argv=argv, input=input)
        return R()
    host = LinuxHost()
    host.helper_argv = ['sudo', '-n', '/opt/adsb-monitor/venv/bin/python', '-I', '/opt/adsb-monitor/helper.py']
    monkeypatch.setattr('system.init_adapter.subprocess.run', fake_run)
    assert host.elevated('write', appmod.READSB_DEFAULT, '--backup', stdin=b'X=1\n').ok
    assert seen['argv'] == [*host.helper_argv, 'write', appmod.READSB_DEFAULT, '--backup']
    assert seen['input'] == b'X=1\n'


def test_root_host_validates_in_process():
    host = LinuxHost()
    host.helper_argv = None
    r = host.elevated('write', '/etc/passwd', stdin=b'x')
    assert r.code == 77 and 'refused' in r.err


def test_refusal_reason_is_logged_not_returned(caplog):
    """err can reach a client (service output, check messages), so it never carries
    the reason, which may echo the request; the log does."""
    host = LinuxHost()
    host.helper_argv = None
    r = host.elevated('write', '/etc/secret-name', stdin=b'x')
    assert r.code == 77 and r.err == REFUSED_MSG and 'secret-name' not in r.err
    assert 'secret-name' in caplog.text


def test_helper_refusal_reason_is_logged_not_returned(monkeypatch, caplog):
    class R:
        returncode, stdout, stderr = 77, b'', b'refused: not a file adsb-monitor manages: /etc/secret-name\n'
    host = LinuxHost()
    host.helper_argv = ['sudo', '-n', 'helper']
    monkeypatch.setattr('system.init_adapter.subprocess.run', lambda *a, **k: R())
    r = host.elevated('write', '/etc/secret-name', stdin=b'x')
    assert r.code == 77 and r.err == REFUSED_MSG
    assert 'secret-name' in caplog.text


def test_write_and_rm_act_on_the_allow_listed_path(fake_host):
    """The gateway resolves the request to its own entry and uses that, so a
    different spelling of a managed path never reaches write_raw or rm."""
    spelled = os.path.dirname(appmod.READSB_DEFAULT) + '/./' + os.path.basename(appmod.READSB_DEFAULT)
    go(fake_host, 'write', spelled, stdin=b'RECEIVER_OPTIONS=""\n')
    assert fake_host.files[appmod.READSB_DEFAULT] == 'RECEIVER_OPTIONS=""\n'
    assert spelled not in fake_host.files
    go(fake_host, 'rm', spelled)
    assert ('run', ('rm', '-f', appmod.READSB_DEFAULT)) in fake_host.calls


def test_unprivileged_write_outside_data_dir_raises_when_refused(monkeypatch):
    host = LinuxHost()
    host.helper_argv = ['false']
    monkeypatch.setattr(host, 'elevated', lambda *a, **k: Result(77, '', 'refused: no'))
    with pytest.raises(PermissionError):
        host.write_text('/etc/default/readsb', 'X=1\n')


def test_helper_main_requires_root(monkeypatch, capsys):
    monkeypatch.setattr(os, 'geteuid', lambda: 1000)
    assert privileged.main(['systemctl', 'daemon-reload']) == 126


def test_helper_main_reports_refusals(monkeypatch, capsys):
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    assert privileged.main(['sh', '-c', 'id']) == 77
    assert 'refused' in capsys.readouterr().err


# ── config import ───────────────────────────────────────────────────────────
def test_config_import_never_restores_the_monitor_unit(fake_host):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('adsb-monitor.service', '[Service]\nUser=root\nExecStart=/bin/sh -c id\n')
        zf.writestr('readsb', 'RECEIVER_OPTIONS="--net-only"\n')
    buf.seek(0)
    c = appmod.app.test_client()
    r = c.post('/api/restore', data={'file': (buf, 'b.zip')}, content_type='multipart/form-data').get_json()
    assert r['restored'] == ['readsb']
    assert '/etc/systemd/system/adsb-monitor.service' not in fake_host.files


@pytest.mark.parametrize('name', ['readsb', 'dump978-fa', 'a@b.service', '-x', '', 'a b', 'a;b', 'x/y'])
def test_unit_name_check_matches_services(name):
    from system.services import valid_unit_name
    assert privileged.valid_unit_name(name) == valid_unit_name(name)


def test_read_verbs_do_not_import_flask():
    import subprocess
    import sys
    code = ("import sys; sys.path.insert(0, '.'); import system.privileged; "
            "print('flask' in sys.modules)")
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert out.stdout.strip() == 'False', out.stderr
