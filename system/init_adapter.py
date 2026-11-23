"""Host + init-system adapters.

The ONE place the app talks to the operating system: running commands and
reading/writing files. Every other function goes through the `HOST` singleton
(defined in app.py) instead of calling subprocess/open directly. That single
seam is what lets the test suite swap in a FakeHost and exercise all the
logic without a real Linux box. `Result` is a tiny value object bundling
(exit code, stdout, stderr). Nothing here raises — failures degrade to
Result(ok=False) / None, matching the monitor's graceful-degradation
behaviour everywhere. See CONTEXT.md ("Host").

Different distros control services differently: systemd uses `systemctl`,
Alpine/minimal SBCs use OpenRC's `rc-service`, and some hosts have neither.
Each adapter exposes the same three operations — status / action (start,
stop, restart) / running_since — so the rest of the app never branches on
init system. `detect_init` probes the host once at startup and picks the
right one. NullAdapter is the graceful fallback (monitor-only mode) that
reports "service control unavailable".
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class Result:
    __slots__ = ('code', 'out', 'err')
    def __init__(self, code: int = 0, out: str = '', err: str = ''):
        self.code, self.out, self.err = code, out, err
    @property
    def ok(self) -> bool:
        return self.code == 0

BACKUP_SUFFIX = '.adsb-monitor.bak'

def _atomic_write(target: str, data: bytes, like: os.stat_result | None) -> None:
    """Write `data` to `target` via a same-directory temp file + fsync + rename."""
    d = os.path.dirname(target) or '.'
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f'.{os.path.basename(target)}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if like is not None:
            os.chmod(tmp, like.st_mode & 0o7777)
            try:
                os.chown(tmp, like.st_uid, like.st_gid)
            except PermissionError:
                pass  # not root: keep our own uid; the mode is still copied
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    try:
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)   # make the rename itself durable
        finally:
            os.close(dfd)
    except OSError:
        pass


class LinuxHost:
    def run(self, cmd: list[str], timeout: int = 10) -> Result:
        try:
            r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
            return Result(r.returncode, r.stdout, r.stderr)
        except Exception:
            # debug, not exception: detect_init deliberately probes for
            # binaries that are absent on this host, so a miss is expected.
            logger.debug("Command failed: %s", cmd[0] if cmd else '', exc_info=True)
            return Result(1, '', 'command failed')
    def read_text(self, path: str) -> str | None:
        try:
            with open(path) as f:
                return f.read()
        except Exception:
            return None
    def read_json(self, path: str) -> Any | None:
        try:
            import json
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None
    def mtime(self, path: str) -> float | None:
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None
    def http_json(self, url: str, timeout: float = 3) -> Any | None:
        """GET a local status endpoint (e.g. fr24feed's monitor.json); None on any failure."""
        try:
            import json
            import urllib.request
            with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (fixed local URLs only)
                return json.loads(r.read().decode('utf-8', 'replace'))
        except Exception:
            return None
    # ── privilege boundary (system/privileged.py) ──────────────────────────
    # helper_argv is None when running as root: elevated() then validates and
    # acts in-process. As the dedicated user it is the sudo command line of
    # /opt/adsb-monitor/helper.py, which runs the same checks as root.
    helper_argv: list[str] | None = None

    def __init__(self) -> None:
        if os.geteuid() != 0:
            self.helper_argv = ['sudo', '-n', '/opt/adsb-monitor/venv/bin/python', '-I',
                                '/opt/adsb-monitor/helper.py']

    def elevated(self, verb: str, *args: str, stdin: bytes = b'', timeout: int = 60) -> Result:
        """A privileged request (see system/privileged.py); refused requests are Result(77)."""
        if self.helper_argv is None:
            from system import privileged
            try:
                return privileged.dispatch(self, verb, list(args), stdin)
            except privileged.Refused as e:
                logger.warning('refused privileged request %s: %s', verb, e)
                return Result(77, '', f'refused: {e}')
        try:
            r = subprocess.run([*self.helper_argv, verb, *args], input=stdin, capture_output=True,
                               timeout=timeout)
            return Result(r.returncode, r.stdout.decode('utf-8', 'replace'),
                          r.stderr.decode('utf-8', 'replace'))
        except Exception:
            logger.warning('privileged helper failed: %s', verb, exc_info=True)
            return Result(1, '', 'privileged helper failed')

    def elevated_argv(self, verb: str, *args: str) -> list[str]:
        """argv for a streaming privileged command (docker-logs)."""
        if self.helper_argv is None:
            from system import privileged
            return privileged.docker_logs_argv(args[0], int(args[1]))
        return [*self.helper_argv, verb, *args]

    def _own(self, path: str) -> bool:
        """True for the monitor's own data files, which it writes directly."""
        import app
        data = os.path.realpath(app.DATA_DIR)
        return os.path.commonpath([data, os.path.realpath(path)]) == data

    def makedirs(self, path: str) -> None:
        if self._own(path):
            os.makedirs(path, exist_ok=True)
        elif not os.path.isdir(path):
            r = self.elevated('mkdir', path)
            if not r.ok:
                raise PermissionError(r.err.strip() or f'mkdir {path} failed')

    def write_text(self, path: str, data: str, backup: bool = False) -> None:
        self.write_bytes(path, data.encode('utf-8'), backup=backup)

    def write_bytes(self, path: str, data: bytes, backup: bool = False) -> None:
        """Write a file: the monitor's own data directly, anything else through
        the privileged gateway (validated, then write_raw as root)."""
        if self._own(path):
            self.write_raw(path, data, backup=backup)
            return
        r = self.elevated('write', path, *(['--backup'] if backup else []), stdin=data)
        if not r.ok:
            raise PermissionError(r.err.strip() or f'write {path} failed')

    def write_raw(self, path: str, data: bytes, backup: bool = False) -> None:
        """Replace `path` atomically: temp file in the same dir, fsync, rename.

        A power cut or crash leaves either the old file or the new one, never a
        truncated mix. Mode and owner are copied from the file being replaced (new
        files get 0644). Writes go to the symlink's target, not over the link.
        backup=True first keeps the current contents as `<path>.adsb-monitor.bak`
        (one last-good copy, itself written atomically) when they differ.
        """
        target = os.path.realpath(path)
        try:
            st: os.stat_result | None = os.stat(target)
        except FileNotFoundError:
            st = None
        if backup and st is not None:
            with open(target, 'rb') as f:
                old = f.read()
            if old != data:
                _atomic_write(target + BACKUP_SUFFIX, old, st)
        _atomic_write(target, data, st)

class InitAdapter:
    """Base: status/action/running_since for a named OS service."""
    def status(self, service: str) -> tuple[str, str]:
        raise NotImplementedError
    def sub_state(self, service: str) -> str:
        """Finer-grained state than status(), or '' where the init system has none.

        Only systemd distinguishes the two flavours of 'activating' that matter to
        the SDR guard: 'start' (legitimately coming up) vs 'auto-restart' (crash
        looping). Defaults to '' so other adapters need not implement it.
        """
        return ''
    def action(self, service: str, act: str) -> tuple[bool, str]:
        raise NotImplementedError
    def running_since(self, service: str) -> str | None:
        raise NotImplementedError

class SystemdAdapter(InitAdapter):
    def __init__(self, host: LinuxHost):
        self._host = host
    def status(self, service: str) -> tuple[str, str]:
        r = self._host.run(['systemctl', 'is-active', service], timeout=3)
        state = r.out.strip()
        return ('ok' if state == 'active' else 'error'), state
    def sub_state(self, service: str) -> str:
        r = self._host.run(['systemctl', 'show', '-p', 'SubState', '--value', service], timeout=3)
        return r.out.strip()
    def action(self, service: str, act: str) -> tuple[bool, str]:
        r = self._host.elevated('systemctl', act, service, timeout=60)
        return r.ok, r.out + r.err
    def running_since(self, service: str) -> str | None:
        r = self._host.run(
            ['systemctl', 'show', service, '--property=ActiveEnterTimestamp'], timeout=3)
        line = r.out.strip()
        if '=' not in line:
            return None
        ts_str = line.split('=', 1)[1].strip()
        if not ts_str or ts_str == 'n/a':
            return None
        for fmt in ['%a %Y-%m-%d %H:%M:%S %Z', '%a %Y-%m-%d %H:%M:%S UTC']:
            try:
                dt = datetime.strptime(ts_str, fmt)
                elapsed = time.time() - dt.replace(tzinfo=timezone.utc).timestamp()
                if elapsed < 0:
                    elapsed = time.time() - dt.timestamp()
                d, rem = divmod(int(abs(elapsed)), 86400)
                h, rem = divmod(rem, 3600)
                m = rem // 60
                if d > 0:
                    return f'{d}d {h}h'
                elif h > 0:
                    return f'{h}h {m}m'
                return f'{m}m'
            except ValueError:
                continue
        return None

class OpenRCAdapter(InitAdapter):
    def __init__(self, host: LinuxHost):
        self._host = host
    def status(self, service: str) -> tuple[str, str]:
        r = self._host.run(['rc-service', service, 'status'], timeout=3)
        out = r.out.strip()
        return ('ok' if 'started' in out else 'error'), out
    def action(self, service: str, act: str) -> tuple[bool, str]:
        r = self._host.run(['rc-service', service, act], timeout=10)
        return r.ok, r.out + r.err
    def running_since(self, service: str) -> str | None:
        return None

class NullAdapter(InitAdapter):
    _MSG = 'service control unavailable — init system not detected'
    def status(self, service: str) -> tuple[str, str]:
        return 'error', self._MSG
    def action(self, service: str, act: str) -> tuple[bool, str]:
        return False, self._MSG
    def running_since(self, service: str) -> str | None:
        return None

def detect_init(host: LinuxHost) -> InitAdapter:
    """Probe the host to pick the right InitAdapter."""
    if host.run(['systemctl', '--version'], timeout=3).ok:
        return SystemdAdapter(host)
    if host.run(['rc-service', '--version'], timeout=3).ok:
        return OpenRCAdapter(host)
    return NullAdapter()
