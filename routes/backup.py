"""Backup/restore routes: config zip + collectd RRD graph history."""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import zipfile
from datetime import datetime
from typing import Any

from flask import Blueprint, jsonify, request, send_file

import app
from system.auth import admin_required
from system.sdr_settings import BIASTEE_1090_CONF

logger = logging.getLogger(__name__)

bp = Blueprint('backup', __name__)

# Named config files in the backup. The name (zip arcname) maps back to an exact
# absolute path on restore — a whitelist, so a tampered zip can't write elsewhere.
CONFIG_BACKUP_FILES = {
    'feeders.ini':          app.CONFIG_FILE,
    'airspy_adsb':          app.AIRSPY_DEFAULT,
    'readsb':               app.READSB_DEFAULT,
    'dump978-fa':           app.DUMP978_DEFAULT,
    'skyaware978':          '/etc/default/skyaware978',
    'fr24feed.ini':         '/etc/fr24feed.ini',
    'piaware.conf':         '/etc/piaware.conf',
    'readsb-biastee.conf':  BIASTEE_1090_CONF,
    'adsb-monitor.service': '/etc/systemd/system/adsb-monitor.service',
}
# In the download for reference, never restored from an upload: the unit names
# the user and command the monitor runs as (system/privileged.py).
RESTORE_SKIP = {'adsb-monitor.service'}
# collectd RRD base. We back up the whole tree (the host-named subdir varies:
# graphs1090 may use 'localhost' or the real hostname), so the graph data is
# captured regardless, and restored to the same place.
GRAPHS_RRD_DIR = '/var/lib/collectd/rrd'


@bp.route('/api/backup')
@admin_required
def api_backup() -> Any:
    """Download a zip of all config files."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, path in CONFIG_BACKUP_FILES.items():
            try:
                zf.write(path, name)
            except Exception:
                pass
    buf.seek(0)
    fname = f'adsb-config-{datetime.now().strftime("%Y%m%d-%H%M%S")}.zip'
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True, download_name=fname)

@bp.route('/api/backup/graphs')
@admin_required
def api_backup_graphs() -> Any:
    """Download a zip of the collectd RRD graph history."""
    if not os.path.isdir(GRAPHS_RRD_DIR):
        return jsonify({'ok': False, 'error': 'no graph data found'}), 404
    buf = io.BytesIO()
    base = os.path.realpath(GRAPHS_RRD_DIR)
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, fnames in os.walk(base):
            for fn in fnames:
                full = os.path.join(root, fn)
                zf.write(full, os.path.relpath(full, base))
    buf.seek(0)
    fname = f'adsb-graphs-{datetime.now().strftime("%Y%m%d-%H%M%S")}.zip'
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True, download_name=fname)

@bp.route('/api/restore', methods=['POST'])
@admin_required
def api_restore() -> Any:
    """Restore config files from an uploaded backup zip (whitelisted names only)."""
    f = request.files.get('file')
    if not f:
        return jsonify({'ok': False, 'error': 'no file uploaded'}), 400
    restored: list[str] = []
    skipped: list[str] = []
    try:
        with zipfile.ZipFile(f.stream) as zf:
            for name in zf.namelist():
                dest = CONFIG_BACKUP_FILES.get(name)
                if not dest or name in RESTORE_SKIP:
                    continue  # ignore anything not in the whitelist
                with zf.open(name) as src:
                    data = src.read()
                try:
                    app.HOST.makedirs(os.path.dirname(dest))
                    # atomic, and the file being replaced is kept as <dest>.adsb-monitor.bak
                    app.HOST.write_bytes(dest, data, backup=True)
                except PermissionError as e:   # refused by the privileged gateway
                    skipped.append(f'{name}: {e}')
                    continue
                restored.append(name)
    except zipfile.BadZipFile:
        return jsonify({'ok': False, 'error': 'not a valid backup zip'}), 400
    except Exception:
        logger.exception("Config restore failed")
        return jsonify({'ok': False, 'error': 'An internal error has occurred.'}), 500
    # Re-apply: reload units (biastee drop-in) and bounce the decoders. Not adsb-monitor
    # itself — that would kill this request before the response is sent.
    app.HOST.elevated('systemctl', 'daemon-reload')
    for svc in ('readsb', 'dump978-fa'):
        app.HOST.elevated('systemctl', 'try-restart', svc)
    out: dict[str, Any] = {'ok': True, 'restored': restored}
    if skipped:
        out['skipped'] = skipped
    return jsonify(out)

def _safe_path_under_base(base_dir: str, archive_name: str, require_rrd: bool = False) -> tuple[str, ...] | None:
    """Return validated relative path parts for an archive entry, or None if invalid."""
    base_real = os.path.realpath(base_dir)
    entry = archive_name.replace('\\', '/')
    if entry.endswith('/'):
        return None
    parts = [p for p in entry.split('/') if p not in ('', '.')]
    if not parts or any(p == '..' for p in parts) or entry.startswith('/'):
        return None
    # Restrict each segment to a conservative safe character set.
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) for p in parts):
        return None
    if require_rrd:
        # Graph restores must be collectd-style nested paths with a .rrd leaf.
        # Per-segment character safety is already enforced above, so here we only
        # check structure: nesting depth and a non-empty stem before '.rrd'. No
        # regex on the leaf — '[A-Za-z0-9_.-]+\.rrd' backtracks polynomially on
        # hostile input (the class overlaps the literal), and adds nothing the
        # checks above don't already cover.
        if len(parts) < 2:
            return None
        leaf = parts[-1]
        if not leaf.endswith('.rrd') or len(leaf) <= len('.rrd'):
            return None
    dest = os.path.realpath(os.path.join(base_real, *parts))
    if os.path.commonpath([base_real, dest]) != base_real:
        return None
    return tuple(parts)


@bp.route('/api/restore/graphs', methods=['POST'])
@admin_required
def api_restore_graphs() -> Any:
    """Restore collectd RRD graph history from an uploaded zip. Stops collectd first."""
    f = request.files.get('file')
    if not f:
        return jsonify({'ok': False, 'error': 'no file uploaded'}), 400
    r = app.HOST.elevated('restore-graphs', stdin=f.stream.read(), timeout=600)
    if r.ok:
        return jsonify({'ok': True, 'restored': int(r.out.strip() or 0)})
    if 'BadZipFile' in r.err:
        return jsonify({'ok': False, 'error': 'not a valid graph backup zip'}), 400
    logger.error('Graph restore failed: %s', r.err.strip()[:200])
    return jsonify({'ok': False, 'error': 'An internal error has occurred.'}), 500


def restore_graphs_zip(host: Any, stream: Any) -> int:
    """Extract a graph backup into GRAPHS_RRD_DIR with collectd stopped. Runs as
    root (system/privileged.py). Raises zipfile.BadZipFile for a bad upload."""
    base = os.path.realpath(GRAPHS_RRD_DIR)
    count = 0
    host.run(['systemctl', 'stop', 'collectd'])
    try:
        os.makedirs(base, exist_ok=True)
        with zipfile.ZipFile(stream) as zf:
            for name in zf.namelist():
                safe_parts = _safe_path_under_base(base, name, require_rrd=True)
                if not safe_parts:
                    continue
                dest = os.path.realpath(os.path.join(base, *safe_parts))
                parent_dir = os.path.realpath(os.path.join(base, *safe_parts[:-1]))
                if os.path.commonpath([base, parent_dir]) != base:
                    continue
                os.makedirs(parent_dir, exist_ok=True)
                # Refuse to follow symlinks at write target.
                if os.path.islink(dest):
                    continue
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                if hasattr(os, 'O_NOFOLLOW'):
                    flags |= os.O_NOFOLLOW
                fd = os.open(dest, flags, 0o644)
                try:
                    with zf.open(name) as src, os.fdopen(fd, 'wb') as out:
                        shutil.copyfileobj(src, out)
                except Exception:
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                    raise
                count += 1
    finally:
        host.run(['systemctl', 'start', 'collectd'])
    host.run(['systemctl', 'try-restart', 'graphs1090'])
    return count
