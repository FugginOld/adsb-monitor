"""LinuxHost.write_text/write_bytes: atomic replace, mode kept, optional last-good backup."""
import os
import stat

import pytest

from system import init_adapter
from system.init_adapter import BACKUP_SUFFIX, LinuxHost

host = LinuxHost()


@pytest.fixture(autouse=True)
def tmp_is_data_dir(tmp_path, monkeypatch):
    """The monitor's own data dir is written directly; anything else goes through
    the privileged gateway (tests/test_privileged.py)."""
    import app as appmod
    monkeypatch.setattr(appmod, 'DATA_DIR', str(tmp_path))


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_replaces_contents_and_keeps_mode(tmp_path):
    p = tmp_path / 'readsb'
    p.write_text('OLD\n')
    os.chmod(p, 0o640)
    host.write_text(str(p), 'NEW\n')
    assert p.read_text() == 'NEW\n'
    assert mode(p) == 0o640
    assert not (tmp_path / ('readsb' + BACKUP_SUFFIX)).exists()


def test_new_file_gets_0644(tmp_path):
    p = tmp_path / 'fresh.conf'
    host.write_text(str(p), 'x=1\n')
    assert p.read_text() == 'x=1\n' and mode(p) == 0o644


def test_backup_keeps_previous_contents(tmp_path):
    p = tmp_path / 'airspy_adsb'
    p.write_text('GAIN=13\n')
    os.chmod(p, 0o600)
    host.write_text(str(p), 'GAIN=14\n', backup=True)
    b = tmp_path / ('airspy_adsb' + BACKUP_SUFFIX)
    assert p.read_text() == 'GAIN=14\n'
    assert b.read_text() == 'GAIN=13\n' and mode(b) == 0o600
    host.write_text(str(p), 'GAIN=15\n', backup=True)
    assert b.read_text() == 'GAIN=14\n'   # one last-good copy: the version just replaced


def test_backup_skipped_when_unchanged_or_new(tmp_path):
    p = tmp_path / 'feeders.ini'
    host.write_text(str(p), 'a\n', backup=True)          # new file: nothing to back up
    assert not (tmp_path / ('feeders.ini' + BACKUP_SUFFIX)).exists()
    host.write_text(str(p), 'a\n', backup=True)          # identical: keep the older backup
    assert not (tmp_path / ('feeders.ini' + BACKUP_SUFFIX)).exists()


def test_writes_through_a_symlink_to_its_target(tmp_path):
    real = tmp_path / 'real.conf'
    real.write_text('old')
    link = tmp_path / 'link.conf'
    link.symlink_to(real)
    host.write_text(str(link), 'new', backup=True)
    assert link.is_symlink() and real.read_text() == 'new'
    assert (tmp_path / ('real.conf' + BACKUP_SUFFIX)).read_text() == 'old'


def test_failed_rename_leaves_original_and_no_temp(tmp_path, monkeypatch):
    p = tmp_path / 'readsb'
    p.write_text('GOOD\n')
    def boom(src, dst):
        raise OSError('disk full')
    monkeypatch.setattr(init_adapter.os, 'replace', boom)
    with pytest.raises(OSError):
        host.write_text(str(p), 'HALF')
    assert p.read_text() == 'GOOD\n'
    assert not [x.name for x in tmp_path.iterdir() if x.name.endswith('.tmp')]


def test_write_bytes_is_byte_exact(tmp_path):
    p = tmp_path / 'fr24feed.ini'
    data = b'receiver="beast-tcp"\r\nhost="127.0.0.1:30005"\n\xff'
    host.write_bytes(str(p), data)
    assert p.read_bytes() == data


def test_missing_directory_raises(tmp_path):
    with pytest.raises(OSError):
        host.write_text(str(tmp_path / 'nope' / 'x.conf'), 'x')


# ── callers ask for the backup ───────────────────────────────────────────────
def test_airspy_settings_save_requests_a_backup(fake_host):
    import app as appmod
    fake_host.files = {appmod.AIRSPY_DEFAULT: 'GAIN=13\nG=-g\n'}
    appmod.write_airspy_options({'gain': '15', 'sample_rate': '12', 'options': ''}, model='mini')
    assert appmod.AIRSPY_DEFAULT in fake_host.backups


def test_config_restore_writes_via_host_with_backup(fake_host):
    import io
    import zipfile
    import app as appmod
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('readsb', 'RECEIVER_OPTIONS="--net-only"\n')
        zf.writestr('../../etc/passwd', 'nope')          # not whitelisted: ignored
    buf.seek(0)
    c = appmod.app.test_client()
    r = c.post('/api/restore', data={'file': (buf, 'backup.zip')}, content_type='multipart/form-data')
    assert r.status_code == 200 and r.get_json()['restored'] == ['readsb']
    assert fake_host.files[appmod.READSB_DEFAULT] == 'RECEIVER_OPTIONS="--net-only"\n'
    assert fake_host.backups == [appmod.READSB_DEFAULT]
