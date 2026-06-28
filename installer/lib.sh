#!/bin/bash

# ─────────────────────────────────────────────────────────────────────────────
# lib.sh — shared helpers sourced by every installer script.
#
# Centralizes the things that must work across distros so the install/update/
# uninstall scripts stay portable: colored logging (info/ok/warn/err), CPU
# architecture detection, a package-manager abstraction (apt/dnf/yum/pacman/
# zypper/apk), a TUI bootstrap (whiptail or a dialog shim), and a distro support
# check. Source this file — do not execute it directly.
# ─────────────────────────────────────────────────────────────────────────────

# Colored status logging used everywhere (==> info, ✓ ok, ⚠ warn, ✗ err).
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}==>${NC} $1"; }
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
err()  { echo -e "${RED}✗${NC} $1"; }

# ── Architecture detection ──────────────────────────────────────────────────
detect_arch() {
    case "$(uname -m)" in
        aarch64) echo "arm64" ;;
        armv7l)  echo "armhf" ;;
        x86_64)  echo "amd64" ;;
        *)       echo "unsupported" ;;
    esac
}
ARCH=$(detect_arch)

# ── Package manager detection ───────────────────────────────────────────────
detect_pkg_manager() {
    if   command -v apt-get &>/dev/null; then echo "apt"
    elif command -v dnf     &>/dev/null; then echo "dnf"
    elif command -v yum     &>/dev/null; then echo "yum"
    elif command -v pacman  &>/dev/null; then echo "pacman"
    elif command -v zypper  &>/dev/null; then echo "zypper"
    elif command -v apk     &>/dev/null; then echo "apk"
    else echo "unknown"; fi
}
PKG_MGR=$(detect_pkg_manager)

pkg_update() {
    case "$PKG_MGR" in
        apt)    apt-get update -qq ;;
        dnf)    dnf check-update -q || true ;;
        yum)    yum check-update -q || true ;;
        pacman) pacman -Sy --noconfirm >/dev/null ;;
        zypper) zypper refresh -q ;;
        apk)    apk update -q ;;
        *)      warn "Unknown package manager — skipping update" ;;
    esac
}

pkg_install() {
    case "$PKG_MGR" in
        apt)    apt-get install -y "$@" >/dev/null 2>&1 ;;
        dnf)    dnf install -y "$@" >/dev/null 2>&1 ;;
        yum)    yum install -y "$@" >/dev/null 2>&1 ;;
        pacman) pacman -S --noconfirm "$@" >/dev/null 2>&1 ;;
        zypper) zypper install -y "$@" >/dev/null 2>&1 ;;
        apk)    apk add "$@" >/dev/null 2>&1 ;;
        *)      err "Unknown package manager — cannot install: $*"; return 1 ;;
    esac
}

pkg_remove() {
    case "$PKG_MGR" in
        apt)    apt-get remove -y "$@" >/dev/null 2>&1 ;;
        dnf)    dnf remove -y "$@" >/dev/null 2>&1 ;;
        yum)    yum remove -y "$@" >/dev/null 2>&1 ;;
        pacman) pacman -R --noconfirm "$@" >/dev/null 2>&1 ;;
        zypper) zypper remove -y "$@" >/dev/null 2>&1 ;;
        apk)    apk del "$@" >/dev/null 2>&1 ;;
        *)      warn "Unknown package manager — cannot remove: $*" ;;
    esac
}

# Install a .deb file; warns and returns 1 on non-apt systems.
dpkg_install() {
    local label="$1" path="$2"
    if [ "$PKG_MGR" != "apt" ]; then
        warn "  $label requires a .deb package — only supported on Debian/Ubuntu/RPi OS"
        return 1
    fi
    dpkg -i "$path" >/dev/null 2>&1
}

# Install build + runtime deps for the full ADS-B stack, mapped per distro.
install_base_deps() {
    case "$PKG_MGR" in
        apt)
            pkg_install git curl wget build-essential cmake pkg-config \
              libusb-1.0-0-dev netcat-openbsd python3 python3-venv python3-pip \
              sqlite3 jq usbutils ;;
        dnf|yum)
            pkg_install git curl wget gcc gcc-c++ make cmake pkgconfig \
              libusb1-devel nmap-ncat python3 python3-pip \
              sqlite jq usbutils ;;
        pacman)
            pkg_install git curl wget base-devel cmake \
              libusb openbsd-netcat python python-pip \
              sqlite jq usbutils ;;
        apk)
            pkg_install git curl wget build-base cmake pkgconfig \
              libusb-dev netcat-openbsd python3 py3-pip \
              sqlite jq usbutils ;;
        *)
            warn "Unknown package manager — install build deps manually" ;;
    esac
}

# ── TUI bootstrap ───────────────────────────────────────────────────────────
# Ensures whiptail or dialog is available. Exports a whiptail→dialog shim if
# whiptail is absent and dialog is present. Returns 1 if neither can be found.
# ────────────────────────────────────────────────────────────────────────────

ensure_tui() {
    command -v whiptail >/dev/null 2>&1 && return 0
    if command -v dialog >/dev/null 2>&1; then
        whiptail() { dialog "$@"; }
        export -f whiptail
        return 0
    fi
    # Try to install whiptail
    case "$PKG_MGR" in
        apt)         pkg_install whiptail ;;
        dnf|yum)     pkg_install newt ;;
        pacman)      pkg_install libnewt ;;
        apk)         pkg_install newt ;;
        *)           err "Cannot install whiptail/dialog — TUI unavailable"; return 1 ;;
    esac
    command -v whiptail >/dev/null 2>&1
}

# ── Distro compatibility check ──────────────────────────────────────────────
# Reads /etc/os-release, prints a support status, and prompts to continue if
# the distro is experimental or unsupported (unless ADSB_FORCE=1).
# ────────────────────────────────────────────────────────────────────────────

check_distro_support() {
    local os_id="" os_like="" status=""
    [ -f /etc/os-release ] && . /etc/os-release
    os_id="${ID:-unknown}"
    os_like="${ID_LIKE:-}"

    case "$os_id" in
        debian|raspbian|ubuntu|armbian) status="supported" ;;
        fedora|centos|rhel|rocky|alma)  status="experimental" ;;
        arch|manjaro)                   status="experimental" ;;
        alpine)                         status="experimental" ;;
        *)
            case "$os_like" in
                *debian*|*ubuntu*) status="supported" ;;
                *)                 status="unsupported" ;;
            esac ;;
    esac

    case "$status" in
        supported)
            ok "Distro: ${PRETTY_NAME:-$os_id} — supported" ;;
        experimental)
            warn "Distro: ${PRETTY_NAME:-$os_id} — experimental (not fully tested)"
            if [ "${ADSB_FORCE:-}" != "1" ]; then
                printf "Continue anyway? [y/N] "; read -r _confirm
                [ "$_confirm" = "y" ] || [ "$_confirm" = "Y" ] || exit 1
            fi ;;
        unsupported)
            err "Distro: ${PRETTY_NAME:-$os_id} — not a supported platform"
            if [ "${ADSB_FORCE:-}" != "1" ]; then
                printf "Continue anyway? [y/N] "; read -r _confirm
                [ "$_confirm" = "y" ] || [ "$_confirm" = "Y" ] || exit 1
            fi ;;
    esac
}
