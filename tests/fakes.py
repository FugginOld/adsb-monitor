"""Test doubles for HOST and INIT — kept out of the production module.

Imported by conftest's fake_host fixture and tests that build their own doubles.
"""
import json

from app import InitAdapter, Result


class FakeHost:
    """Test double: answers from canned maps, records every call, reflects writes.

    commands: {(cmd, tuple): Result}   files: {path: text-or-obj}
    Unmatched run -> default_command (a failed Result); unmatched read -> default_file.
    """
    def __init__(self, commands=None, files=None, default_command=None, default_file=None):
        self.commands = {tuple(k): v for k, v in (commands or {}).items()}
        self.files = dict(files or {})
        self.default_command = default_command if default_command is not None else Result(1, '', '')
        self.default_file = default_file
        self.calls = []
        self.writes = {}
    def run(self, cmd, timeout=10):
        key = tuple(cmd)
        self.calls.append(('run', key))
        return self.commands.get(key, self.default_command)
    def read_text(self, path):
        self.calls.append(('read_text', path))
        return self.files.get(path, self.default_file)
    def read_json(self, path):
        self.calls.append(('read_json', path))
        raw = self.files.get(path, self.default_file)
        if raw is None:
            return None
        return json.loads(raw) if isinstance(raw, str) else raw
    def write_text(self, path, data):
        self.calls.append(('write_text', path, data))
        self.writes[path] = data
        self.files[path] = data


class FakeInitAdapter(InitAdapter):
    """Test double for INIT — mirrors FakeHost pattern."""
    def __init__(self, statuses=None, actions=None, since=None):
        self.statuses = statuses or {}
        self.actions  = actions  or {}
        self.since    = since    or {}
        self.calls    = []
    def status(self, service):
        self.calls.append(('status', service))
        return self.statuses.get(service, ('error', 'unknown'))
    def action(self, service, act):
        self.calls.append(('action', service, act))
        return self.actions.get(service, (False, 'not configured'))
    def running_since(self, service):
        self.calls.append(('running_since', service))
        return self.since.get(service)
