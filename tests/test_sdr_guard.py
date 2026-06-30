"""enforce_sdr_presence: stop a decoder when its SDR is unplugged, resume on replug.

Covers RTL sticks (readsb 1090, dump978 978) probed via sysfs serials and Airspy
probed via lsusb. Auto-resume fires only for a service the guard itself stopped.
"""
import pytest

import app as appmod
from app import Result

RTL1090 = 'RECEIVER_OPTIONS="--device-type rtlsdr --device 00001090 --gain auto"'
RTL978  = 'RECEIVER_OPTIONS="--sdr driver=rtlsdr,serial=00000978"'
USBSER  = ('/bin/sh', '-c', 'cat /sys/bus/usb/devices/*/serial 2>/dev/null')


@pytest.fixture(autouse=True)
def _clear_autostopped():
    appmod._sdr_autostopped.clear()
    yield
    appmod._sdr_autostopped.clear()


def _did(fake, service, act):
    return ('run', ('systemctl', act, service)) in fake.calls


# ── RTL 1090 (readsb) ────────────────────────────────────────────────────────
def test_1090_absent_stops_readsb(fake_host):
    fake_host.files = {appmod.READSB_DEFAULT: RTL1090}
    fake_host.commands = {
        ('systemctl', 'is-active', 'readsb'): Result(3, 'failed', ''),
        USBSER: Result(0, '00000978\n', ''),   # only the 978 stick
    }
    appmod.enforce_sdr_presence()
    assert _did(fake_host, 'readsb', 'stop')
    assert 'readsb' in appmod._sdr_autostopped


def test_1090_present_left_alone(fake_host):
    fake_host.files = {appmod.READSB_DEFAULT: RTL1090}
    fake_host.commands = {
        ('systemctl', 'is-active', 'readsb'): Result(0, 'inactive', ''),
        USBSER: Result(0, '00001090\n', ''),
    }
    appmod.enforce_sdr_presence()
    assert not _did(fake_host, 'readsb', 'stop')


def test_active_decoder_not_stopped(fake_host):
    # Busy stick can't be read by rtl_eeprom — an active decoder is proof it's present.
    fake_host.files = {appmod.READSB_DEFAULT: RTL1090}
    fake_host.commands = {('systemctl', 'is-active', 'readsb'): Result(0, 'active', '')}
    appmod.enforce_sdr_presence()
    assert not _did(fake_host, 'readsb', 'stop')


# ── 978 (dump978-fa): unplug then replug ─────────────────────────────────────
def test_978_unplug_then_resume(fake_host):
    fake_host.files = {appmod.DUMP978_DEFAULT: RTL978}
    fake_host.commands = {
        ('systemctl', 'is-active', 'dump978-fa'): Result(3, 'failed', ''),
        USBSER: Result(0, '00001090\n', ''),   # 978 stick gone
    }
    appmod.enforce_sdr_presence()
    assert _did(fake_host, 'dump978-fa', 'stop')

    # Plug it back in: stick now visible, service settled inactive -> resume.
    fake_host.calls.clear()
    fake_host.commands = {
        ('systemctl', 'is-active', 'dump978-fa'): Result(0, 'inactive', ''),
        USBSER: Result(0, '00001090\n00000978\n', ''),
    }
    appmod.enforce_sdr_presence()
    assert _did(fake_host, 'dump978-fa', 'start')
    assert 'dump978-fa' not in appmod._sdr_autostopped


def test_978_not_resumed_if_manually_stopped(fake_host):
    # Stick present, service inactive, but we never stopped it -> don't start it.
    fake_host.files = {appmod.DUMP978_DEFAULT: RTL978}
    fake_host.commands = {
        ('systemctl', 'is-active', 'dump978-fa'): Result(0, 'inactive', ''),
        USBSER: Result(0, '00000978\n', ''),
    }
    appmod.enforce_sdr_presence()
    assert not _did(fake_host, 'dump978-fa', 'start')


# ── Airspy (airspy_adsb) via lsusb ───────────────────────────────────────────
def test_airspy_absent_stops_then_resumes(fake_host):
    fake_host.files = {appmod.AIRSPY_DEFAULT: 'OPTIONS=""'}
    fake_host.commands = {
        ('systemctl', 'is-active', 'airspy_adsb'): Result(3, 'failed', ''),
        ('lsusb',): Result(0, 'Bus 001 Device 002: ID 1234:5678 Something', ''),
        ('airspy_info',): Result(1, '', 'No AirSpy boards found'),
    }
    appmod.enforce_sdr_presence()
    assert _did(fake_host, 'airspy_adsb', 'stop')

    fake_host.calls.clear()
    fake_host.commands = {
        ('systemctl', 'is-active', 'airspy_adsb'): Result(0, 'inactive', ''),
        ('lsusb',): Result(0, 'Bus 001 Device 004: ID 1d50:60a1 AirSpy', ''),
    }
    appmod.enforce_sdr_presence()
    assert _did(fake_host, 'airspy_adsb', 'start')


def test_no_airspy_config_skipped(fake_host):
    # AIRSPY_DEFAULT absent -> _airspy_present returns None -> never touched.
    fake_host.commands = {('systemctl', 'is-active', 'airspy_adsb'): Result(3, 'failed', '')}
    appmod.enforce_sdr_presence()
    assert not _did(fake_host, 'airspy_adsb', 'stop')
