"""Status / probe: thin wrappers that delegate service status and
start/stop/restart actions to the active INIT adapter (and to docker for
container feeders).

`HOST` and `INIT` stay defined in app.py, reached via `import app`.
"""
import app


def systemd_status(service):
    return app.INIT.status(service)

def docker_status(container):
    r = app.HOST.run(['docker', 'inspect', '--format', '{{.State.Status}}', container], timeout=5)
    state = r.out.strip()
    if not state: return 'error', 'not found'
    return ('ok' if state == 'running' else 'error'), state

def service_action(service, action):
    return app.INIT.action(service, action)
