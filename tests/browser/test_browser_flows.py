"""Settings and status flows in a real browser (see conftest.py here).

Each test also fails on any page error, console error or CSP violation, which
is what catches a broken data-click wiring or an inline handler slipping back.
"""
import os
import re
import threading

import pytest
from playwright.sync_api import expect

import app as appmod
from system import apply_guard, dataflow
from system.init_adapter import BACKUP_SUFFIX

pytestmark = pytest.mark.timeout(90)


def open_settings(page):
    page.click('#btn-settings')
    expect(page.locator('#view-settings')).to_be_visible()


def finish_check_later(monkeypatch, state, message, after=1.0):
    """Instead of a real verify thread, end each check with `state` after `after` s."""
    def start(check):
        threading.Timer(after, apply_guard._set, args=(check, state, message)).start()
    monkeypatch.setattr(apply_guard, '_start', start)


def test_dashboard_renders_cards_and_metrics(page, station):
    expect(page.locator('#core-cards .card')).to_have_count(2)
    expect(page.locator('#feeder-cards .card')).to_have_count(2)
    expect(page.locator('#feeder-cards')).to_contain_text('FlightRadar24')
    assert page.problems == []


def test_view_switching_and_theme(page, station):
    open_settings(page)
    page.click('#btn-map')
    expect(page.locator('#view-settings')).to_be_hidden()
    before = page.evaluate('document.documentElement.dataset.theme || ""')
    page.click('[data-click="toggleTheme"]')
    assert page.evaluate('document.documentElement.dataset.theme || ""') != before
    assert page.problems == []


def test_airspy_save_follows_the_check_to_verified(page, station, monkeypatch):
    finish_check_later(monkeypatch, 'verified', 'airspy_adsb restarted and is running with the new settings')
    open_settings(page)
    expect(page.locator('#airspy-gain')).to_have_value('13')
    page.select_option('#airspy-gain', '15')
    page.click('[data-click="saveAirspy"]')
    toast = page.locator('#airspy-toast')
    expect(toast).to_contain_text('checking that airspy_adsb comes back')
    expect(toast).to_contain_text('restarted and is running with the new settings', timeout=15000)
    assert 'GAIN=15' in station['host'].files[appmod.AIRSPY_DEFAULT]
    assert page.problems == []


def test_rolled_back_save_shows_why(page, station, monkeypatch):
    finish_check_later(monkeypatch, 'rolled_back',
                       'readsb is failed 60 s after the save; restored the previous settings and restarted readsb')
    open_settings(page)
    page.fill('#recv-range', '250')
    page.click('[data-click="saveReceiver"]')
    toast = page.locator('#recv-toast')
    expect(toast).to_contain_text('restored the previous settings', timeout=15000)
    expect(toast).to_have_class(re.compile(r'\berr\b'))
    assert page.problems == []


def test_revert_from_backup(page, station, monkeypatch):
    host = station['host']
    host.files[appmod.READSB_DEFAULT] = 'RECEIVER_OPTIONS="--net-only --bad"\n'
    host.files[os.path.realpath(appmod.READSB_DEFAULT) + BACKUP_SUFFIX] = 'RECEIVER_OPTIONS="--net-only"\n'
    finish_check_later(monkeypatch, 'verified', 'readsb restarted and is running with the new settings')
    dialogs = []
    page.on('dialog', lambda d: (dialogs.append(d.message), d.accept()))
    open_settings(page)
    row = page.locator('#config-backups .backup-row', has_text='readsb')
    expect(row).to_have_count(1)
    row.locator('[data-click="revertConfig"]').click()
    expect(page.locator('#revert-toast')).to_contain_text('restarted and is running', timeout=15000)
    assert dialogs and appmod.READSB_DEFAULT in dialogs[0]
    assert host.files[appmod.READSB_DEFAULT] == 'RECEIVER_OPTIONS="--net-only"\n'
    assert host.files[os.path.realpath(appmod.READSB_DEFAULT) + BACKUP_SUFFIX] == 'RECEIVER_OPTIONS="--net-only --bad"\n'
    assert page.problems == []


def test_feeder_list_edit_is_saved(page, station):
    open_settings(page)
    label = page.locator('#feeder-rows [data-change="feederField"][data-field="label"]').nth(2)
    expect(label).to_have_value('FlightRadar24')
    label.fill('FR24 (edited)')
    label.dispatch_event('change')
    page.click('[data-click="saveFeeders"]')
    expect(page.locator('#feeders-toast')).to_contain_text('Saved')
    # saved through the host seam, so the new feeders.ini lands in the FakeHost
    assert 'label = FR24 (edited)' in station['host'].files[str(station['ini'])]
    assert page.problems == []


def test_running_feeder_without_data_shows_badge_and_banner(page, station, monkeypatch):
    real = dataflow.current
    def fake_current(feeder):
        if feeder['key'] == 'fr24feed':
            return {'state': 'stale', 'detail': 'FR24 link disconnected (no data for 6m)',
                    'age_s': None, 'since': 1.0, 'mlat': None}
        return real(feeder)
    monkeypatch.setattr(dataflow, 'current', fake_current)
    page.reload()
    card = page.locator('#feeder-cards .card', has_text='FlightRadar24')
    expect(card.locator('.badge')).to_have_text('no data', timeout=15000)
    expect(page.locator('#alert-items')).to_contain_text('No data: FlightRadar24 (FR24 link disconnected', timeout=40000)
    assert page.problems == []


def test_labels_from_feeders_ini_are_escaped(page, station):
    station['ini'].write_text(station['ini'].read_text().replace(
        'label = adsb.fi', 'label = <img src=x onerror="window.__pwned=1">adsb.fi'))
    page.reload()
    expect(page.locator('#feeder-cards')).to_contain_text('<img src=x')
    assert page.evaluate('window.__pwned === undefined')
    assert page.problems == []
