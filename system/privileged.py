"""The one gateway for actions that need root.

When adsb-monitor runs as root (the default install), `HOST.elevated(verb, ...)`
calls `dispatch()` in-process. When it runs as the dedicated `adsb-monitor`
user (installer/service-user.sh), the same call goes through
`sudo /opt/adsb-monitor/venv/bin/python -I /opt/adsb-monitor/helper.py <verb> ...`,
which runs `main()` below as root. Either way every request passes the same
checks, so the web process can only ask for:

  ping                             no-op (checks the sudo path works)
  systemctl  start|stop|restart|try-restart|reset-failed <unit>, daemon-reload
  write      <path> [--backup]     contents on stdin; path and content validated
  rm         <path>                a managed path only
  mkdir      <dir>                 the directory of a managed path only
  piaware-config <key> <value>     the piaware fields the dashboard edits
  docker-inspect <container> [<format>]   fixed formats only
  docker-logs    <container> <lines>      (helper only: execs `docker logs -f`)
  docker-recreate <container>      env updates (JSON) on stdin; image, hosts and
                                   flags come from the existing container
  stack-update   <component>       one of system/stack_update.COMPONENTS
  restore-graphs                   collectd RRD zip on stdin

Content checks matter as much as paths: a root service sources each
/etc/default file, and the bias-tee drop-in's ExecStartPre is a root command,
so a write may not carry shell syntax, and the drop-in must be one of the two
templates the dashboard generates. The adsb-monitor unit file is never
writable this way (it could name any user and ExecStart).

Residual, by design: the caller can stop or restart services (not systemd's
own or rescue units), and can set decoder options that the decoders then
parse. It cannot run a command of its choosing as root.
"""
from __future__ import annotations

import io
import json
import os
import string
import sys
from typing import Any

from system.init_adapter import Result

# `app` (and Flask with it) is imported only by the verbs that need it, so the
# helper's frequent read-only calls (docker-inspect from the poller) start fast.

HELPER = '/opt/adsb-monitor/helper.py'
MAX_STDIN = 512 * 1024 * 1024        # graph backups can be large

_UNIT_DENY = ('debug-shell', 'emergency', 'rescue')
_SYSTEMCTL_ACTS = ('start', 'stop', 'restart', 'try-restart', 'reset-failed')
_NAME_CHARS = frozenset(string.ascii_letters + string.digits + '_')
_UNIT_CHARS = frozenset(string.ascii_letters + string.digits + '_.@-')   # as system/services.py
_SERIAL_CHARS = frozenset(string.ascii_letters + string.digits)
_UUID_CHARS = frozenset(string.hexdigits + '-')
# characters that would let a value do more than be a value when a shell sources the file
_SHELL_META = ('`', '$(', '${', ';', '|', '&', '<', '>', '\\', '\r')
_DOCKER_FORMATS = (
    '{{.State.Status}}',
    '{{.State.StartedAt}}',
    '{{if .State.Health}}{{.State.Health.Status}}{{end}}',
    '{{range .Config.Env}}{{println .}}{{end}}',
)


class Refused(ValueError):
    pass


# ── what may be written ────────────────────────────────────────────────────
def writable_paths() -> dict[str, str]:
    """Managed root-owned files -> content kind ('env', 'ini', 'piaware', 'biastee', 'uuid')."""
    import app
    from routes.backup import CONFIG_BACKUP_FILES
    from system.config_io import FEEDER_CONFIGS
    from system.sdr_settings import BIASTEE_1090_CONF
    kinds: dict[str, str] = {
        app.AIRSPY_DEFAULT: 'env', app.READSB_DEFAULT: 'env', app.DUMP978_DEFAULT: 'env',
        '/etc/default/skyaware978': 'env', BIASTEE_1090_CONF: 'biastee',
        '/etc/fr24feed.ini': 'ini', '/etc/piaware.conf': 'piaware',
    }
    for cfg in FEEDER_CONFIGS.values():
        if cfg.get('config_file'):
            kinds.setdefault(cfg['config_file'],
                             'env' if cfg.get('format') == 'shell_vars' else 'ini')
        for fpath in cfg.get('extra_files', {}).values():
            kinds.setdefault(fpath, 'uuid')
    for dest in CONFIG_BACKUP_FILES.values():
        if dest in kinds or _in_data_dir(dest):
            continue
        if not dest.endswith('adsb-monitor.service'):
            raise RuntimeError(f'backup file {dest} has no content kind')   # keep the lists in step
    return kinds


def _in_data_dir(path: str) -> bool:
    import app
    data = os.path.realpath(app.DATA_DIR)
    return os.path.commonpath([data, os.path.realpath(path)]) == data


def _resolve(path: str) -> tuple[str, str]:
    """(path to act on, kind) for a managed file or its .adsb-monitor.bak; Refused otherwise.

    The path returned comes from writable_paths(), never from the caller, so the
    verbs below only ever touch a file on the allow-list."""
    from system.init_adapter import BACKUP_SUFFIX
    if not path or '\0' in path:
        raise Refused('bad path')
    kinds = writable_paths()
    real = os.path.realpath(path)
    for managed, kind in kinds.items():
        target = os.path.realpath(managed)
        if real == target:
            return managed, kind
        if real == target + BACKUP_SUFFIX:
            return target + BACKUP_SUFFIX, kind
    raise Refused(f'not a file adsb-monitor manages: {path}')


def _check_env_line(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith('#'):
        return True
    name, eq, value = s.partition('=')
    if not eq or not name or name[0].isdigit() or not set(name) <= _NAME_CHARS:
        return False
    return not any(m in value for m in _SHELL_META)


def _check_biastee(text: str) -> bool:
    head = '[Service]\nExecStartPre='
    if not text.startswith(head) or not text.endswith('\n'):
        return False
    pre = text[len(head):-1]
    if pre == '/bin/sh -c "rtl_biast -d 0 -b 1; true"':
        return True
    start = '/bin/sh -c "for i in 0 1 2 3; do rtl_eeprom -d $i 2>/dev/null | grep -q '
    end = ' && { rtl_biast -d $i -b 1; break; }; done; true"'
    if pre.startswith(start) and pre.endswith(end):
        serial = pre[len(start):-len(end)]
        return 0 < len(serial) <= 32 and set(serial) <= _SERIAL_CHARS
    return False


def validate_content(kind: str, data: bytes) -> None:
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise Refused('not UTF-8 text') from None
    if '\0' in text:
        raise Refused('binary content')
    if kind == 'biastee':
        if not _check_biastee(text):
            raise Refused('bias-tee drop-in is not one adsb-monitor generates')
        return
    if kind == 'uuid':
        s = text.strip()
        if not s or len(s) > 64 or not set(s) <= _UUID_CHARS or '\n' in s:
            raise Refused('not a UUID')
        return
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if kind == 'env':
            ok = _check_env_line(line)
        elif kind == 'ini':
            ok = (not s or s[0] in '#;' or (s.startswith('[') and s.endswith(']'))
                  or ('=' in s and not any(m in s for m in ('`', '$(', '\r'))))
        else:   # piaware: `key value` lines, parsed by Tcl
            ok = not s or s.startswith('#') or not any(c in s for c in '`$[];\\\r')
        if not ok:
            raise Refused(f'line {n} is not allowed in this file')


# ── verbs ──────────────────────────────────────────────────────────────────
def valid_unit_name(name: str) -> bool:
    return bool(name) and not name.startswith('-') and set(name) <= _UNIT_CHARS


def _unit(name: str) -> str:
    base = name[:-len('.service')] if name.endswith('.service') else name
    if (not valid_unit_name(name) or '.' in base or base in _UNIT_DENY
            or base.startswith('systemd-')):
        raise Refused(f'unit not allowed: {name}')
    return name


def _container(name: str) -> str:
    if not valid_unit_name(name):
        raise Refused('bad container name')
    return name


def _systemctl(host: Any, args: list[str]) -> Result:
    if args == ['daemon-reload']:
        return host.run(['systemctl', 'daemon-reload'], timeout=30)
    if len(args) != 2 or args[0] not in _SYSTEMCTL_ACTS:
        raise Refused('systemctl: action not allowed')
    return host.run(['systemctl', args[0], _unit(args[1])], timeout=60)


def _write(host: Any, args: list[str], stdin: bytes) -> Result:
    backup = '--backup' in args
    paths = [a for a in args if a != '--backup']
    if len(paths) != 1:
        raise Refused('write: one path')
    path, kind = _resolve(paths[0])
    validate_content(kind, stdin)
    host.write_raw(path, stdin, backup=backup)
    return Result(0, '', '')


def _rm(host: Any, args: list[str]) -> Result:
    if len(args) != 1:
        raise Refused('rm: one path')
    path, _kind = _resolve(args[0])
    return host.run(['rm', '-f', path])


def _mkdir(host: Any, args: list[str]) -> Result:
    if len(args) != 1:
        raise Refused('mkdir: one path')
    want = os.path.realpath(args[0])
    if want not in {os.path.dirname(os.path.realpath(p)) for p in writable_paths()}:
        raise Refused('mkdir: not a managed directory')
    return host.run(['mkdir', '-p', want])


def _piaware_config(host: Any, args: list[str]) -> Result:
    from system.config_io import FEEDER_CONFIGS
    fields = {f['key'] for cfg in FEEDER_CONFIGS.values()
              if cfg.get('write_via_cmd') for f in cfg.get('fields', []) if not f.get('readonly')}
    if len(args) != 2 or args[0] not in fields:
        raise Refused('piaware-config: field not allowed')
    value = args[1]
    if not value or len(value) > 200 or value.startswith('-') or any(c in value for c in '`$[];\\\n\r '):
        raise Refused('piaware-config: value not allowed')
    return host.run(['piaware-config', args[0], value], timeout=10)


def _docker_inspect(host: Any, args: list[str]) -> Result:
    if len(args) == 1:
        return host.run(['docker', 'inspect', _container(args[0])], timeout=10)
    if len(args) == 2 and args[1] in _DOCKER_FORMATS:
        return host.run(['docker', 'inspect', '--format', args[1], _container(args[0])], timeout=10)
    raise Refused('docker-inspect: format not allowed')


def docker_logs_argv(container: str, lines: int) -> list[str]:
    return ['docker', 'logs', '-f', '--tail', str(int(lines)), '--timestamps', _container(container)]


def _docker_recreate(host: Any, args: list[str], stdin: bytes) -> Result:
    from system.config_io import FEEDER_CONFIGS, recreate_container
    if len(args) != 1 or args[0] not in {c.get('docker_container') for c in FEEDER_CONFIGS.values()}:
        raise Refused('docker-recreate: not a managed container')
    try:
        env = json.loads(stdin or b'{}')
    except ValueError:
        raise Refused('docker-recreate: env must be JSON') from None
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and k and set(k) <= _NAME_CHARS
            and '\n' not in v and '\0' not in v for k, v in env.items()):
        raise Refused('docker-recreate: env must map NAME to a one-line value')
    ok, msg = recreate_container(host, args[0], env)
    return Result(0 if ok else 1, msg, '' if ok else msg)


def _stack_update(host: Any, args: list[str]) -> Result:
    from system import stack_update
    if len(args) != 1 or args[0] not in stack_update.COMPONENTS:
        raise Refused('stack-update: unknown component')
    return stack_update.launch(host, args[0])


def _restore_graphs(host: Any, args: list[str], stdin: bytes) -> Result:
    from routes.backup import restore_graphs_zip
    if args:
        raise Refused('restore-graphs: no arguments')
    try:
        count = restore_graphs_zip(host, io.BytesIO(stdin))
    except Refused:
        raise
    except Exception as e:     # zipfile.BadZipFile etc.
        return Result(2, '', f'{type(e).__name__}')
    return Result(0, str(count), '')


def dispatch(host: Any, verb: str, args: list[str], stdin: bytes = b'') -> Result:
    """Validate and perform one privileged request on `host` (a root LinuxHost
    in the helper; the app's HOST when running as root). Raises Refused."""
    if verb == 'ping':                       # installer/service-user.sh checks sudo with this
        return Result(0, 'ok\n', '')
    if verb == 'systemctl':
        return _systemctl(host, args)
    if verb == 'write':
        return _write(host, args, stdin)
    if verb == 'rm':
        return _rm(host, args)
    if verb == 'mkdir':
        return _mkdir(host, args)
    if verb == 'piaware-config':
        return _piaware_config(host, args)
    if verb == 'docker-inspect':
        return _docker_inspect(host, args)
    if verb == 'docker-recreate':
        return _docker_recreate(host, args, stdin)
    if verb == 'stack-update':
        return _stack_update(host, args)
    if verb == 'restore-graphs':
        return _restore_graphs(host, args, stdin)
    raise Refused(f'unknown verb: {verb}')


# ── helper entry point (root, via sudo) ────────────────────────────────────
_READ_VERBS = ('ping', 'docker-inspect', 'docker-logs')


def _audit(verb: str, args: list[str], code: int) -> None:
    """Log each request that changes something to the auth log (sudo logs only refusals)."""
    if verb in _READ_VERBS:
        return
    shown = args[:1] if verb == 'piaware-config' else args    # values may be feeder IDs
    try:
        import syslog
        syslog.openlog('adsb-monitor-helper', 0, syslog.LOG_AUTH)
        syslog.syslog(syslog.LOG_NOTICE, f'{verb} {" ".join(shown)} -> exit {code}')
    except Exception:
        pass


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        print('adsb-monitor helper: must run as root (via sudo)', file=sys.stderr)
        return 126
    if not argv:
        print('usage: helper.py <verb> [args...]', file=sys.stderr)
        return 64
    verb, args = argv[0], argv[1:]
    try:
        if verb == 'docker-logs':
            if len(args) != 2 or not args[1].isdigit():
                raise Refused('docker-logs: <container> <lines>')
            cmd = docker_logs_argv(args[0], int(args[1]))
            os.execvp(cmd[0], cmd)
        stdin = b''
        if verb in ('write', 'docker-recreate', 'restore-graphs'):
            stdin = sys.stdin.buffer.read(MAX_STDIN + 1)
            if len(stdin) > MAX_STDIN:
                raise Refused('input too large')
        from system.init_adapter import LinuxHost
        r = dispatch(LinuxHost(), verb, args, stdin)
        _audit(verb, args, r.code)
    except Refused as e:
        _audit(verb, [], 77)
        print(f'refused: {e}', file=sys.stderr)
        return 77
    sys.stdout.write(r.out)
    sys.stderr.write(r.err)
    return r.code
