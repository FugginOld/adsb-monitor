"""Entry point for the ADS-B monitor server.

Deliberately separate from app.py: system/*.py modules do `import app` to
reach HOST/INIT/DB_FILE, so app.py must only ever be imported as the module
`app`, never executed directly as `__main__` (that would run it twice under
two different module names and crash on a circular import). Point
systemd's ExecStart at this file, not app.py.

`run.py set-password` sets the admin-port password instead of starting the
server (prompts twice; writes admin_auth.json next to app.py, mode 0600).
"""
import getpass
import sys
import threading

import app


def set_password_cli() -> int:
    from system import admin_login
    print(f'Setting the admin password for port {app.ADMIN_PORT} ({app.ADMIN_AUTH_FILE}).')
    print(f'At least {admin_login.MIN_LENGTH} characters. Logs out every current session.')
    pw = getpass.getpass('New admin password: ')
    if pw != getpass.getpass('Repeat it: '):
        print('Passwords do not match; nothing changed.', file=sys.stderr)
        return 1
    if len(pw) < admin_login.MIN_LENGTH:
        print(f'Too short (minimum {admin_login.MIN_LENGTH} characters); nothing changed.', file=sys.stderr)
        return 1
    try:
        admin_login.set_password(pw)
    except OSError as e:
        print(f'Not set: could not write {app.ADMIN_AUTH_FILE} ({e.strerror}).', file=sys.stderr)
        return 1
    print('Admin password set. It takes effect immediately; no restart needed.')
    return 0


if __name__ == '__main__' and sys.argv[1:] == ['set-password']:
    sys.exit(set_password_cli())

if __name__ == '__main__':
    app.init_db()
    # Start background threads — version refresh is fully async, never blocks startup
    threading.Thread(target=app.refresh_versions, daemon=True).start()
    threading.Thread(target=app.background_poll,  daemon=True).start()
    threading.Thread(target=app.run_server, args=(app.READONLY_PORT,), daemon=True).start()
    app.run_server(app.ADMIN_PORT)
