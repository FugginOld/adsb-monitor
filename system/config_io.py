"""Feeder configs + config store (read/write adapters).

Every feeder stores its settings differently — flat INI, shell-style
KEY=val /etc/default files, piaware-config, or env vars baked into a
docker container. FEEDER_CONFIGS is a declarative table describing, per
feeder, where its config lives, which format it uses, and which fields the
UI may edit. The read_*/write_* helpers below implement each format once;
the table wires feeders to them so adding a feeder is data, not new code.

The write side: each format has a `_write_*` function with the same
(cfg, data) -> (ok, message) shape, paired with its reader in the
CONFIG_ADAPTERS table. `_config_adapter` picks the right one for a feeder,
and get_/set_feeder_settings are the two public entry points the routes
call. `_write_docker` recreates the container atomically: it renames the
old one aside, runs the new one, and only deletes the backup on success
(rolling back on failure) so a bad value can never leave the feeder gone.
See CONTEXT.md ("Config store").

Loads the feeder list from feeders.ini — the [service:<unit>] /
[docker:<name>] sections that define what shows up in the sidebar.
`load_config` parses them, `get_config_map` indexes by key, and
`save_feeders` writes the list back when the user edits sidebar entries in
Settings.

`HOST` and `CONFIG_FILE` stay defined in app.py, reached via `import app`.
"""
import configparser
import io
import json
import logging
from collections import namedtuple

import app

logger = logging.getLogger(__name__)

# FEEDER_CONFIGS is deliberately a separate, smaller registry from
# feeders.ini (loaded by load_config() below) — feeders.ini declares every
# monitored unit (including e.g. airspy_adsb/readsb/tar1090, which have no
# editable settings), while FEEDER_CONFIGS only lists the ones the Settings
# UI can read/write. A feeders.ini entry with no FEEDER_CONFIGS match is
# expected, not a bug: routes/settings.py's get_feeder_cfg checks
# FEEDER_CONFIGS.get(key) first and returns a clear "No config defined for
# this feeder" instead of ever falling through to a missing schema.
FEEDER_CONFIGS = {
    'fr24feed': {
        'label': 'FlightRadar24',
        'config_file': '/etc/fr24feed.ini',
        'format': 'ini_flat',
        'fields': [
            {'key': 'fr24key',   'label': 'Sharing Key',  'type': 'password'},
            {'key': 'host',      'label': 'Beast Host',   'type': 'text',   'default': '127.0.0.1'},
            {'key': 'port',      'label': 'Beast Port',   'type': 'text',   'default': '30005'},
            {'key': 'bs',        'label': 'Beast Format', 'type': 'select', 'options': ['yes','no'], 'default': 'yes'},
            {'key': 'mlat',      'label': 'MLAT',         'type': 'select', 'options': ['yes','no'], 'default': 'no'},
        ],
    },
    'piaware': {
        'label': 'FlightAware (PiAware)',
        'config_cmd_read': ['piaware-config', '--show'],
        'write_via_cmd': True,
        'write_cmd_template': ['piaware-config', '{key}', '{value}'],
        'restart_after_write': True,
        'fields': [
            {'key': 'feeder-id',       'label': 'Feeder ID (UUID)', 'type': 'text'},
            {'key': 'receiver-host',   'label': 'Receiver Host',    'type': 'text', 'default': 'localhost'},
            {'key': 'receiver-port',   'label': 'Receiver Port',    'type': 'text', 'default': '30005'},
            {'key': 'flightaware-user','label': 'FlightAware User', 'type': 'text'},
        ],
    },
    'adsbexchange-feed': {
        'label': 'ADSBExchange',
        'config_file': '/etc/default/adsbexchange-feed',
        'format': 'shell_vars',
        'extra_files': {'UUID': '/usr/local/share/adsbexchange/adsbexchange-uuid'},
        'fields': [
            {'key': 'UUID',        'label': 'Station UUID', 'type': 'text', 'readonly': True, 'source': 'extra'},
            {'key': 'ADSBX_USER', 'label': 'Username',     'type': 'text'},
            {'key': 'LAT',         'label': 'Latitude',    'type': 'text'},
            {'key': 'LONG',        'label': 'Longitude',   'type': 'text'},
            {'key': 'ALT',         'label': 'Altitude (ft)','type': 'text'},
        ],
    },
    'adsbfi-feed': {
        'label': 'adsb.fi',
        'extra_files': {'UUID': '/usr/local/share/adsbfi/adsbfi-uuid'},
        'fields': [
            {'key': 'UUID', 'label': 'Station UUID', 'type': 'text', 'readonly': True, 'source': 'extra'},
        ],
    },
    'airnavradar': {
        'label': 'AirNav Radar',
        'docker_container': 'airnavradar',
        'fields': [
            {'key': 'SHARING_KEY', 'label': 'Sharing Key', 'type': 'password'},
            {'key': 'LAT',         'label': 'Latitude',    'type': 'text'},
            {'key': 'LONG',        'label': 'Longitude',   'type': 'text'},
            {'key': 'ALT',         'label': 'Altitude (m)','type': 'text'},
        ],
    },
}

def read_flat_ini(path):
    vals = {}
    for line in (app.HOST.read_text(path) or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        if '=' in line:
            k, _, v = line.partition('=')
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals

def write_flat_ini(path, data):
    app.HOST.write_text(path, ''.join(f'{k}={v}\n' for k, v in data.items()))

def read_shell_vars(path):
    vals = {}
    for line in (app.HOST.read_text(path) or '').splitlines():
        line = line.strip().lstrip('export').strip()
        if not line or line.startswith('#'): continue
        if '=' in line:
            k, _, v = line.partition('=')
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals

def write_shell_vars(path, data):
    existing = app.HOST.read_text(path) or ''
    lines = existing.splitlines()
    updated = set()
    out = []
    for line in lines:
        stripped = line.strip().lstrip('export').strip()
        if '=' in stripped and not stripped.startswith('#'):
            k = stripped.split('=')[0].strip()
            if k in data:
                out.append(f'{k}="{data[k]}"')
                updated.add(k)
                continue
        out.append(line)
    for k, v in data.items():
        if k not in updated:
            out.append(f'{k}="{v}"')
    app.HOST.write_text(path, '\n'.join(out) + '\n')

def read_piaware_config(fields):
    vals = {}
    r = app.HOST.run(['piaware-config', '--show'], timeout=5)
    for line in r.out.splitlines():
        if ' ' in line:
            k, _, v = line.partition(' ')
            vals[k.strip()] = v.strip().split()[0]
    return vals

def read_docker_env(container):
    vals = {}
    r = app.HOST.run(
        ['docker', 'inspect', '--format', '{{range .Config.Env}}{{println .}}{{end}}', container],
        timeout=5)
    for line in r.out.splitlines():
        if '=' in line:
            k, _, v = line.partition('=')
            vals[k.strip()] = v.strip()
    return vals

Adapter = namedtuple('Adapter', ['read', 'write'])

def _writable_only(cfg, data):
    """Drop readonly fields — file formats filter, docker/piaware historically don't."""
    writable = {f['key'] for f in cfg.get('fields', []) if not f.get('readonly')}
    return {k: v for k, v in data.items() if k in writable}

def _write_ini_flat(cfg, data):
    existing = read_flat_ini(cfg['config_file'])
    existing.update(_writable_only(cfg, data))
    write_flat_ini(cfg['config_file'], existing)
    return True, 'Saved'

def _write_shell_vars(cfg, data):
    write_shell_vars(cfg['config_file'], _writable_only(cfg, data))
    return True, 'Saved'

def _write_piaware(cfg, data):
    for k, v in data.items():
        if v: app.HOST.run(['piaware-config', k, v], timeout=5)
    return True, 'Saved'

def _write_docker(cfg, data):
    container = cfg['docker_container']
    current = read_docker_env(container)
    current.update({k: v for k, v in data.items() if v != ''})
    r = app.HOST.run(['docker', 'inspect', container], timeout=5)
    info = json.loads(r.out)[0]
    image = info['Config']['Image']
    extra_hosts = info['HostConfig'].get('ExtraHosts') or []
    env_args  = [arg for k, v in current.items() for arg in ['-e', f'{k}={v}']]
    host_args = [arg for h in extra_hosts for arg in ['--add-host', h]]
    # Rename the old container aside instead of deleting it, so a failed
    # `docker run` (bad env value, missing image, ...) can be rolled back —
    # otherwise the feeder is gone with no way back.
    backup = f'{container}_bak'
    app.HOST.run(['docker', 'rm', '-f', backup], timeout=10)   # clear any stale backup
    app.HOST.run(['docker', 'stop', container], timeout=10)
    app.HOST.run(['docker', 'rename', container, backup], timeout=10)
    run = app.HOST.run(['docker', 'run', '-d', '--name', container, '--restart', 'unless-stopped']
                   + env_args + host_args + [image], timeout=15)
    if not run.ok:
        # roll back: restore and restart the original container
        app.HOST.run(['docker', 'rename', backup, container], timeout=10)
        app.HOST.run(['docker', 'start', container], timeout=10)
        return False, run.err or 'docker run failed'
    app.HOST.run(['docker', 'rm', '-f', backup], timeout=10)   # success: drop backup
    return True, 'Container recreated'

CONFIG_ADAPTERS = {
    'ini_flat':   Adapter(lambda cfg: read_flat_ini(cfg['config_file']),         _write_ini_flat),
    'shell_vars': Adapter(lambda cfg: read_shell_vars(cfg['config_file']),       _write_shell_vars),
    'piaware':    Adapter(lambda cfg: read_piaware_config(cfg.get('fields', [])), _write_piaware),
    'docker':     Adapter(lambda cfg: read_docker_env(cfg['docker_container']),  _write_docker),
}

def _config_adapter(cfg):
    """Select the format adapter once — used by both get and set."""
    if 'docker_container' in cfg:
        return CONFIG_ADAPTERS['docker']
    if 'config_file' in cfg:
        return CONFIG_ADAPTERS.get(cfg.get('format', 'ini_flat'))
    if cfg.get('config_cmd_read') or cfg.get('write_via_cmd'):
        return CONFIG_ADAPTERS['piaware']
    return None

def get_feeder_settings(key):
    cfg = FEEDER_CONFIGS.get(key)
    if not cfg: return {}
    adapter = _config_adapter(cfg)
    vals = adapter.read(cfg) if adapter else {}
    for fname, fpath in cfg.get('extra_files', {}).items():
        vals[fname] = (app.HOST.read_text(fpath) or '').strip()
    return {field['key']: vals.get(field['key'], field.get('default', '')) for field in cfg.get('fields', [])}

def set_feeder_settings(key, data):
    cfg = FEEDER_CONFIGS.get(key)
    if not cfg: return False, 'Unknown feeder'
    adapter = _config_adapter(cfg)
    if not adapter:
        return False, 'No write method defined'
    try:
        return adapter.write(cfg, data)
    except Exception:
        logger.exception("Failed to write feeder settings for key=%s", key)
        return False, 'Failed to update feeder settings'

def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(app.CONFIG_FILE)
    items = []
    for section in cfg.sections():
        if ':' not in section: continue
        kind, name = section.split(':', 1)
        if kind not in ('service', 'docker'): continue
        items.append({
            'key': name, 'kind': kind,
            'label': cfg.get(section, 'label', fallback=name),
            'hint':  cfg.get(section, 'hint',  fallback=''),
            'icon':  cfg.get(section, 'icon',  fallback='plug'),
        })
    return items

def get_config_map():
    return {f['key']: f for f in load_config()}

def save_feeders(feeders):
    cfg = configparser.ConfigParser()
    for f in feeders:
        section = f'{f["kind"]}:{f["key"]}'
        cfg[section] = {'label': f.get('label', f['key']), 'hint': f.get('hint', ''), 'icon': f.get('icon', 'plug')}
    buf = io.StringIO()
    buf.write('# ADS-B Stack Monitor - Feeder Configuration\n')
    buf.write('# Section types: [service:<unit>] or [docker:<container>]\n\n')
    cfg.write(buf)
    app.HOST.write_text(app.CONFIG_FILE, buf.getvalue())
