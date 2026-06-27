# Cross-Platform Support Plan

Goal: make adsb-monitor work on all Raspberry Pis, SBCs, and major Linux distros —
not just RPi 5 / ARM64 / Debian/Armbian.

## Tested Target Platforms

| Platform | Arch | Distro | Init |
|---|---|---|---|
| Raspberry Pi 5 | ARM64 | RPi OS / Armbian | systemd |
| Raspberry Pi 4 | ARM64 / ARMhf | RPi OS / Armbian | systemd |
| Raspberry Pi 3 / Zero 2 W | ARMhf | RPi OS Lite | systemd |
| Orange Pi 5 / Rock Pi | ARM64 | Armbian (Rockchip) | systemd |
| Orange Pi / Banana Pi | ARMhf/ARM64 | Armbian (Allwinner) | systemd |
| Beelink / generic x86 mini-PC | x86_64 | Ubuntu / Debian | systemd |
| Generic SBC (Alpine-based) | ARM64 / ARMhf | Alpine Linux | OpenRC |
| Generic x86 server | x86_64 | Fedora / Arch | systemd |

---

## Changes Required

### 1. CPU Temperature Sensor — `app.py:360-363` (Small)

**Problem:** Only checks `cpu_thermal` (RPi ARM) and `coretemp` (Intel x86).
Other SBCs expose temperatures under different psutil sensor keys.

**Fix:** Replace the two-branch check with a prioritized list scan:

```python
TEMP_SENSOR_KEYS = [
    'cpu_thermal',   # Raspberry Pi (ARM)
    'coretemp',      # Intel x86
    'k10temp',       # AMD x86 (Beelink, etc.)
    'acpitz',        # Generic ACPI (x86 mini-PCs)
    'cpu-thermal',   # Rockchip SBCs (Rock Pi, Orange Pi 5)
    'soc-thermal',   # Allwinner SBCs (Orange Pi, Banana Pi)
]
```

Also add a `None` guard around the entire `sensors_temperatures()` call —
it returns `None` on some kernels / permission configurations.

**Files:** `app.py`

---

### 2. Architecture Detection — installer scripts (Medium)

**Problem:** `installer/install-stack.sh:176` hardcodes `arm64` in the pfclient
download URL. No other arch is supported.

**Fix:** Add an arch detection block near the top of install scripts:

```bash
detect_arch() {
    case "$(uname -m)" in
        aarch64) echo "arm64" ;;
        armv7l)  echo "armhf" ;;
        x86_64)  echo "amd64" ;;
        *)       echo "unsupported" ;;
    esac
}
ARCH=$(detect_arch)
```

Use `$ARCH` everywhere a binary or package download URL is constructed.
For components that don't ship an `armhf` or `amd64` build (e.g. pfclient),
emit a warning and skip gracefully instead of failing:

```bash
if [ "$ARCH" = "unsupported" ]; then
    echo "WARNING: pfclient not available for $(uname -m) — skipping"
else
    # download pfclient_..._${ARCH}.deb
fi
```

**Files:** `installer/install-stack.sh`, `installer/update-stack.sh`

---

### 3. Package Manager Abstraction — installer scripts (Medium)

**Problem:** All installer/uninstaller scripts hardcode `apt-get`.
Non-Debian distros (Fedora IoT, Arch Linux ARM, Alpine, openSUSE) use
different package managers and different package names.

**Fix:** Add a package manager detection and wrapper to a shared helper
(e.g. `installer/lib.sh`) sourced by all scripts:

```bash
detect_pkg_manager() {
    if   command -v apt-get &>/dev/null; then echo "apt"
    elif command -v dnf     &>/dev/null; then echo "dnf"
    elif command -v yum     &>/dev/null; then echo "yum"
    elif command -v pacman  &>/dev/null; then echo "pacman"
    elif command -v zypper  &>/dev/null; then echo "zypper"
    elif command -v apk     &>/dev/null; then echo "apk"
    else echo "unknown"; fi
}

pkg_install() {
    case "$PKG_MGR" in
        apt)    apt-get install -y "$@" ;;
        dnf)    dnf install -y "$@" ;;
        yum)    yum install -y "$@" ;;
        pacman) pacman -S --noconfirm "$@" ;;
        zypper) zypper install -y "$@" ;;
        apk)    apk add "$@" ;;
        *)      echo "ERROR: unsupported package manager"; exit 1 ;;
    esac
}

pkg_remove() { ... same pattern ... }
```

Also handle TUI dependency: if `whiptail` is unavailable (Alpine, minimal Arch),
fall back to `dialog`, then to plain `read` prompts.

Package name mapping to verify across distros:

| Logical package | apt | dnf/yum | pacman | apk |
|---|---|---|---|---|
| `usbutils` | `usbutils` | `usbutils` | `usbutils` | `usbutils` |
| `python3-venv` | `python3-venv` | `python3` | `python` | `python3` |
| `whiptail` | `whiptail` | `newt` | `libnewt` | `newt` |

**Files:** `installer/install-stack.sh`, `installer/uninstall.sh`,
`installer/update-stack.sh` — extract shared logic to `installer/lib.sh`

---

### 4. Init System Detection — installer + `app.py` (Large)

**Problem:** All scripts and `app.py` assume systemd (`systemctl`, `/etc/systemd/`).
Alpine uses OpenRC; some minimal SBC images use runit or s6.

#### 4a. Installer scripts

Add detection at install time:

```bash
detect_init() {
    if   command -v systemctl &>/dev/null && systemctl --version &>/dev/null 2>&1; then echo "systemd"
    elif command -v rc-service &>/dev/null; then echo "openrc"
    elif command -v sv         &>/dev/null; then echo "runit"
    else echo "unknown"; fi
}
```

Gate the `systemctl enable/start` calls on `$INIT_SYSTEM = "systemd"`.
For OpenRC, emit the equivalent `rc-update add` / `rc-service start` commands.

#### 4b. `app.py` service control

The service start/stop/restart/status endpoints (around `app.py:754-765`)
call `systemctl` directly via subprocess. Introduce a thin adapter:

```python
class InitAdapter:
    def status(self, service: str) -> str: ...
    def start(self, service: str): ...
    def stop(self, service: str): ...
    def restart(self, service: str): ...

class SystemdAdapter(InitAdapter): ...   # current behavior
class OpenRCAdapter(InitAdapter): ...    # rc-service equivalents
```

Detect init system at startup and inject the right adapter.
If init system is unknown, disable service control endpoints and return
a clear API error: `{"error": "service control unavailable — systemd not detected"}`.

**Files:** `app.py`, `installer/install-stack.sh`, `installer/uninstall.sh`,
new `installer/lib.sh`

---

### 5. Distro Compatibility Check — installer (Small)

**Problem:** `install-stack.sh` header says "For Armbian / Debian on Raspberry Pi 5"
but has no runtime check. Users on unsupported distros get cryptic failures.

**Fix:** Read `/etc/os-release` at installer start and print a support status:

```bash
. /etc/os-release
case "$ID" in
    debian|raspbian|ubuntu|armbian) STATUS="supported" ;;
    fedora|centos|rhel)             STATUS="experimental" ;;
    arch|manjaro)                   STATUS="experimental" ;;
    alpine)                         STATUS="experimental" ;;
    *)                              STATUS="unsupported" ;;
esac

if [ "$STATUS" = "unsupported" ]; then
    echo "WARNING: $PRETTY_NAME is not a tested platform."
    echo "Continue anyway? (y/N)"
    read -r confirm
    [ "$confirm" = "y" ] || exit 1
fi
```

**Files:** `installer/install-stack.sh` (or sourced from `installer/lib.sh`)

---

### 6. README / Documentation (Small)

**Problem:** `installer/README.md` only covers RPi 5 / Armbian.

**Fix:**

- Add a "Supported Platforms" table (see table at top of this file)
- Add a "Platform Notes" section covering known feeder limitations per arch
  (e.g. pfclient arm64-only, airspy_adsb armhf support status)
- Update install commands to be generic (remove RPi 5 specifics from the
  quick-start section)

**Files:** `installer/README.md`

---

## Implementation Order

| # | Change | File(s) | Effort | Status |
| --- | --- | --- | --- | --- |
| 1 | CPU temp sensor list + None guard | `app.py:355-369` | XS | ✅ done |
| 2 | Arch detection variable in installer | `installer/install-stack.sh` | S | ✅ done (via lib.sh) |
| 3 | Extract `installer/lib.sh` with pkg + init helpers | new `installer/lib.sh` | M | ✅ done |
| 4 | Replace all `apt-get` calls with `pkg_install`/`pkg_remove` | all installer scripts | M | ✅ done |
| 5 | Distro compatibility check at installer start | `installer/install-stack.sh` | S | ✅ done (via lib.sh) |
| 6 | `InitAdapter` in `app.py` | `app.py` | L | ✅ done |
| 7 | Wire `OpenRCAdapter` (Alpine / minimal SBC support) | `app.py` | M | ✅ done |
| 8 | Update `installer/README.md` | `installer/README.md` | S | ✅ done |

Start with items 1–5 (pure installer / config changes, no `app.py` logic changes).
Items 6–7 (init adapter) are the largest change and should be TDD'd using the
existing `FakeHost` adapter pattern already in place.
