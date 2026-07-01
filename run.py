"""Entry point for the ADS-B monitor server.

Deliberately separate from app.py: system/*.py modules do `import app` to
reach HOST/INIT/DB_FILE, so app.py must only ever be imported as the module
`app`, never executed directly as `__main__` (that would run it twice under
two different module names and crash on a circular import). Point
systemd's ExecStart at this file, not app.py.
"""
import threading

import app

if __name__ == '__main__':
    app.init_db()
    # Start background threads — version refresh is fully async, never blocks startup
    threading.Thread(target=app.refresh_versions, daemon=True).start()
    threading.Thread(target=app.background_poll,  daemon=True).start()
    threading.Thread(target=app.run_server, args=(app.READONLY_PORT,), daemon=True).start()
    app.run_server(app.ADMIN_PORT)
