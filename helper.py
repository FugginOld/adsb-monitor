"""Root side of the privilege boundary: `sudo -n <venv>/python -I helper.py <verb> ...`.

Only used when adsb-monitor runs as the dedicated `adsb-monitor` user
(installer/service-user.sh). Every request is validated by
system/privileged.py; see that module for the verbs. This file and the code it
imports must stay root-owned and not writable by the service user.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from system.privileged import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
