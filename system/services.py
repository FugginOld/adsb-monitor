"""Status / probe: thin wrappers that delegate service status and
start/stop/restart actions to the active INIT adapter (and to docker for
container feeders).

`HOST` and `INIT` stay defined in app.py, reached via `import app`.

Unit/container names reach here from feeders.ini, which the Settings UI
writes from request JSON - so the "allowlist" a caller is checked against is
one the caller authored. `valid_unit_name` is the chokepoint that closes
that loop: no shell is involved, but without it an admin-authenticated
request can start/stop any unit on the box as root. Enforced again in
config_io.save_feeders so a bad name never lands in the file to begin with.
"""
from __future__ import annotations

import string

import app

# No regex: a character-set test cannot backtrack. See docs/review-policy.md
# ("Do not re-add" - the routes/backup.py leaf-name pattern).
_UNIT_CHARS = frozenset(string.ascii_letters + string.digits + '_.@-')


def valid_unit_name(name: str) -> bool:
    """A systemd unit / OpenRC service / docker container name, nothing else."""
    return bool(name) and not name.startswith('-') and set(name) <= _UNIT_CHARS


def systemd_status(service: str) -> tuple[str, str]:
    return app.INIT.status(service)

def systemd_sub_state(service: str) -> str:
    return app.INIT.sub_state(service)

def docker_status(container: str) -> tuple[str, str]:
    r = app.HOST.run(['docker', 'inspect', '--format', '{{.State.Status}}', container], timeout=5)
    state = r.out.strip()
    if not state: return 'error', 'not found'
    return ('ok' if state == 'running' else 'error'), state

def service_action(service: str, action: str) -> tuple[bool, str]:
    if not valid_unit_name(service):
        return False, 'invalid service name'
    if action not in ('start', 'stop', 'restart'):
        return False, 'invalid action'
    return app.INIT.action(service, action)
