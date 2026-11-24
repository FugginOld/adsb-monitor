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


def set_db_clock(ts):
    """Make system/db.py (only) stamp rows at `ts`; None restores the real clock.

    Patching `db.time.time` would change time.time for the whole process: the
    test server would then see the login cookie as issued in the future, drop
    the session, and the page would fall back to /login mid-test."""
    import time as real
    import types
    from system import db
    db.time = real if ts is None else types.SimpleNamespace(time=lambda: ts)


def seed_timeline(hours=24):
    """24 h of plausible samples with two gain steps and a few other changes."""
    import math
    import time as _t
    from system import db
    now = _t.time()
    try:
        for i in range(int(hours * 3600 / 300)):
            ts = now - hours * 3600 + i * 300
            day = 0.5 + 0.5 * math.sin((ts % 86400) / 86400 * 2 * math.pi - 1.8)
            gain = 15.0 if i < 120 else 13.0 if i < 230 else 14.0
            sig = {'strong_pct': max(0.2, 6.5 - (15 - gain) * 2.2 + day), 'gain': gain,
                   'snr': 12.0, 'noise': 37.0}
            set_db_clock(ts)
            db.record_metrics(int(40 + 80 * day), int(300 + 1500 * day), int(140 + 50 * day), sig)
            if i == 120:
                db.record_gain_change('15', '13', 'auto-tune: 6.5% strong signals > 5% — lowering gain 15 → 13')
            if i == 230:
                db.record_gain_change('13', '14', 'auto-tune: 0.3% strong signals < 0.5% — raising gain 13 → 14')
                db.record_change('restart', 'airspy_adsb', 'airspy_adsb restarted')
            if i == 250:
                db.record_change('config', 'readsb', 'settings save: readsb restarted and is running with the new settings', 'ok')
            if i == 260:
                db.record_dataflow_event('fr24feed', 'stale', 'FR24 link disconnected')
    finally:
        set_db_clock(None)


def test_timeline_view(page, station):
    seed_timeline()
    page.click('#btn-timeline')
    expect(page.locator('#view-timeline')).to_be_visible()
    expect(page.locator('#tl-chart svg .tl-line')).to_have_count(4)
    expect(page.locator('#tl-chart .tl-marker')).to_have_count(2)
    assert sorted(page.locator('#tl-chart .tl-marker-label').all_text_contents()) == ['13→14', '15→13']
    expect(page.locator('#tl-changes .tl-change')).to_have_count(5)
    expect(page.locator('#tl-changes')).to_contain_text('gain 13 → 14')
    expect(page.locator('#tl-links')).to_contain_text('All tracks, last 24 h')
    box = page.locator('#tl-chart .tl-hit').bounding_box()
    page.mouse.move(box['x'] + box['width'] * 0.6, box['y'] + 60)
    expect(page.locator('#tl-tip')).to_be_visible()
    expect(page.locator('#tl-tip')).to_contain_text('Airspy gain')
    page.click('[data-click="setTimelineHours"][data-hours="6"]')
    expect(page.locator('#tl-links')).to_contain_text('last 6 h')
    page.click('[data-click="toggleTimelineTable"]')
    expect(page.locator('#tl-chart table tbody tr').first).to_be_visible()
    shots = os.environ.get('TIMELINE_SHOTS')
    if shots:
        page.click('[data-click="toggleTimelineTable"]')
        page.click('[data-click="setTimelineHours"][data-hours="24"]')
        expect(page.locator('#tl-chart .tl-marker')).to_have_count(2)
        page.mouse.move(box['x'] + box['width'] * 0.6, box['y'] + 60)
        page.screenshot(path=f'{shots}/timeline-light.png', full_page=True)
        page.click('[data-click="toggleTheme"]')
        page.screenshot(path=f'{shots}/timeline-dark.png', full_page=True)
        page.set_viewport_size({'width': 390, 'height': 900})
        page.wait_for_timeout(400)
        page.screenshot(path=f'{shots}/timeline-phone.png', full_page=True)
    assert page.problems == []


def test_timeline_busy_night_keeps_chart_and_labels_apart(page, station):
    """A night of auto-tune steps every 15 min fills the change list past the
    screen; the chart must keep its full height (it used to shrink to 120 px and
    spill over the list) and neither the step labels nor the two threshold
    labels may print on top of each other."""
    import time as _t
    from system import db
    seed_timeline()
    now = _t.time()
    try:
        gains = [13, 14, 15, 16, 15, 14, 13, 14, 15, 16, 15, 14, 15, 14, 15, 14, 13]
        for i, (old, new) in enumerate(zip(gains, gains[1:])):
            ts = now - 18 * 3600 + i * 900
            set_db_clock(ts)
            db.record_gain_change(str(old), str(new), f'auto-tune: lowering/raising gain {old} → {new}')
            set_db_clock(ts + 30)
            db.record_change('restart', 'airspy_adsb', 'airspy_adsb restarted', ref=f'busy-{i}')
        # and one step a minute ago, at the chart's right edge
        set_db_clock(now - 60)
        db.record_gain_change('13', '14', 'auto-tune: raising gain 13 → 14')
    finally:
        set_db_clock(None)
    page.click('#btn-timeline')
    expect(page.locator('#tl-chart .tl-marker')).to_have_count(19)
    assert page.locator('#tl-changes .tl-change').count() > 30

    chart = page.locator('#tl-chart').bounding_box()
    svg = page.locator('#tl-chart svg').bounding_box()
    heading = page.locator('#view-timeline .tl-h').bounding_box()
    assert chart['height'] >= svg['height']
    assert heading['y'] >= svg['y'] + svg['height']

    def no_overlap(boxes, gap=3):
        """Labels on the same row keep at least `gap` px between them."""
        boxes = sorted(boxes, key=lambda b: b['x'])
        for a, b in zip(boxes, boxes[1:]):
            same_row = abs(a['y'] - b['y']) < a['height']
            assert not (same_row and a['x'] + a['width'] + gap > b['x']), (a, b)

    labels = page.locator('#tl-chart .tl-marker-label')
    assert 2 <= labels.count() < 19
    no_overlap([labels.nth(i).bounding_box() for i in range(labels.count())])
    def inside_chart(b, s):
        return s['x'] <= b['x'] and b['x'] + b['width'] <= s['x'] + s['width'] + 0.5

    svg_box = page.locator('#tl-chart svg').bounding_box()
    assert all(inside_chart(labels.nth(i).bounding_box(), svg_box) for i in range(labels.count()))
    assert labels.last.text_content() == '13→14'      # the edge step keeps its label
    thresh = page.locator('#tl-chart .tl-thresh-label')
    expect(thresh).to_have_count(2)
    no_overlap([thresh.nth(i).bounding_box() for i in range(2)])

    # Phone widths: the same labels, and the time axis, still stay apart and
    # inside the chart.
    for w in (390, 320):
        page.set_viewport_size({'width': w, 'height': 844})
        for hours in ('24', '6'):
            page.click(f'[data-click="setTimelineHours"][data-hours="{hours}"]')
            expect(page.locator('#tl-links')).to_contain_text(f'last {hours} h')
            # wait for the redraw at this width (resize re-renders after 150 ms)
            page.wait_for_function("""() => { const b = document.getElementById('tl-chart'),
                                                    s = b.querySelector('svg');
                                              return s && +s.getAttribute('width') === b.clientWidth; }""")
            # one snapshot: the chart re-renders on refresh, so measure in a single call
            m = page.evaluate("""() => {
                const box = e => { const r = e.getBoundingClientRect();
                                   return {x: r.x, y: r.y, width: r.width, height: r.height}; };
                const svg = document.querySelector('#tl-chart svg'), s = box(svg);
                const all = sel => [...svg.querySelectorAll(sel)].map(box);
                return {svg: s, axis: all('text.tl-axis').filter(b => b.y > s.y + s.height - 20),
                        markers: all('.tl-marker-label'), thresh: all('.tl-thresh-label')};
            }""")
            svg, axis = m['svg'], m['axis']
            assert len(axis) >= 2, (w, hours)
            no_overlap(axis)
            assert all(svg['x'] <= b['x'] and b['x'] + b['width'] <= svg['x'] + svg['width'] + 0.5 for b in axis)
            no_overlap(m['markers'])
            assert all(inside_chart(b, svg) for b in m['markers']), (w, hours)
            no_overlap(m['thresh'])
    assert page.problems == []


def test_strong_signal_sparkline(page, station):
    seed_timeline(hours=1)
    page.reload()
    expect(page.locator('#sig-strong')).to_have_text(re.compile(r'^\d+\.\d%$'), timeout=15000)
    expect(page.locator('#spark-strong polyline')).to_have_count(1)
    assert page.problems == []
