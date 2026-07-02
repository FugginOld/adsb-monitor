"""Tests for the version cache's own interface (system/versions.py).

invalidate_cache() closes a seam violation: routes/versions.py used to poke
system.versions._version_ts directly (a private-by-convention module global)
instead of calling through an interface. See ARCHITECTURE.md's versions.py
candidate.
"""
import time

import system.versions as versions


def test_invalidate_cache_resets_version_ts():
    versions._version_ts = time.time()
    versions.invalidate_cache()
    assert versions._version_ts == 0


def test_get_versions_refreshes_after_invalidate_but_not_when_fresh(monkeypatch):
    refreshes = []
    monkeypatch.setattr(versions, 'refresh_versions', lambda: refreshes.append('ran'))

    # freshly populated cache -> get_versions should NOT spawn a refresh
    versions._version_ts = time.time()
    versions.get_versions()
    time.sleep(0.05)  # refresh_versions runs on a daemon thread; give it a beat
    assert refreshes == []

    # invalidated cache -> get_versions SHOULD spawn a refresh
    versions.invalidate_cache()
    versions.get_versions()
    time.sleep(0.05)
    assert refreshes == ['ran']
