"""Log streaming.

Streams live logs to the browser via Server-Sent Events. `_log_command`
builds the follow command (`journalctl -u <unit> -f` for services, `docker
logs -f` for containers); `stream_logs` spawns it and yields each line as an
SSE frame. journalctl is wrapped in `stdbuf -oL` to force line-buffering —
without it, journalctl block-buffers when piped and a quiet unit's stream
looks dead. `_sse` is the one-line JSON framing helper.

`LOG_LINES` stays defined in app.py, reached via `import app`.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Iterator

import app


def _sse(text: str) -> str:
    """Frame one line of text as a Server-Sent Events data message."""
    return f"data: {json.dumps(text)}\n\n"

def _journalctl(*unit_args: str) -> list[str]:
    """Follow-logs journalctl invocation. stdbuf -oL forces line-buffered output:
    journalctl block-buffers when its stdout is a pipe (non-tty), so quiet units
    never flush and the stream looks like it never connected. coreutils stdbuf
    ships on every systemd host."""
    return ['stdbuf', '-oL', '-eL',
            'journalctl', *unit_args, '-f', '-n', str(app.LOG_LINES), '--no-pager', '--output=short-iso']

def _log_command(feeder: dict[str, Any]) -> list[str]:
    """The follow-logs command for a Feeder — the single kind-dispatch for logs."""
    if feeder['kind'] == 'docker':
        return ['docker', 'logs', '-f', '--tail', str(app.LOG_LINES), '--timestamps', feeder['key']]
    return _journalctl('-u', feeder['key'])

def stream_logs(cmd: list[str]) -> Iterator[str]:
    """Spawn a follow-logs process and yield its lines as SSE frames."""
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        yield _sse('[connected]')
        assert proc.stdout is not None
        for line in proc.stdout:
            yield _sse(line.rstrip())
        # stdout closed = the follow process exited on its own (unknown unit, no
        # container, journalctl/docker missing). Surface it instead of looking idle.
        code = proc.wait()
        if code:
            yield _sse(f'[stream ended — exit {code}; unit may have no journal or not exist]')
    except GeneratorExit:
        if proc: proc.kill()
    except Exception as e:
        yield _sse(f'[error] {e}')
    finally:
        if proc:
            try: proc.kill()
            except Exception: pass
