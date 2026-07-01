"""Background poller.

A daemon thread (started in app.py) that runs forever: every 30s it records
each service's status into the events ledger and samples readsb metrics
into the metrics table. This is what populates the uptime history and
sparklines over time, independent of whether anyone has the dashboard open.
"""
import time

from system.config_io import load_config
from system.db import record_metrics, record_service_event
from system.feeders import feeder_status, readsb_metrics
from system.sdr import enforce_sdr_presence


def background_poll():
    """Record service states and metrics to SQLite every 30s."""
    consecutive_errors = 0
    while True:
        try:
            try:
                enforce_sdr_presence()
            except Exception:
                pass
            feeders = load_config()
            for f in feeders:
                try:
                    status, _ = feeder_status(f)
                    record_service_event(f['key'], status)
                except Exception:
                    pass
            m = readsb_metrics()
            record_metrics(m['aircraft'], m['msg_rate'], m['max_range_nm'])
            consecutive_errors = 0
        except Exception:
            consecutive_errors += 1
        # Backoff: 30s normal, up to 5min if errors persist
        sleep_time = min(30 * (2 ** min(consecutive_errors, 4)), 300)
        time.sleep(sleep_time)
