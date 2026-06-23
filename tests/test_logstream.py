"""Tests for the log-stream seam's pure parts: SSE framing + command resolver.

The Popen lifecycle stays out of scope (streaming is not on the Host seam);
these cover the two parts that carry actual logic.
"""
import json

import app as appmod


# ── _sse framing ─────────────────────────────────────────────────────────────
def test_sse_frames_a_line():
    assert appmod._sse('hello') == 'data: "hello"\n\n'


def test_sse_json_escapes():
    out = appmod._sse('a "quote" and \\ slash')
    assert out.startswith('data: ')
    assert out.endswith('\n\n')
    # round-trips through json
    assert json.loads(out[len('data: '):].strip()) == 'a "quote" and \\ slash'


# ── _log_command resolver (kills the kind-ternary) ───────────────────────────
def test_log_command_service():
    cmd = appmod._log_command({'key': 'readsb', 'kind': 'service'})
    assert cmd[:3] == ['journalctl', '-u', 'readsb']
    assert '-f' in cmd


def test_log_command_docker():
    cmd = appmod._log_command({'key': 'airnavradar', 'kind': 'docker'})
    assert cmd[:3] == ['docker', 'logs', '-f']
    assert cmd[-1] == 'airnavradar'
