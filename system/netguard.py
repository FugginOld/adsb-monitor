"""Who may reach the admin port, by client address.

The admin port listens on every interface by default (ADMIN_BIND), because the
LAN address comes from DHCP and Tailscale can come up after the monitor starts:
a fixed bind list would silently miss an interface after a reboot. Instead
every admin-port request is checked against ADMIN_ALLOW_NETS before Flask sees
it (app.make_tagged_app), and anything else gets a plain 403.

ADMIN_ALLOW_NETS: comma-separated CIDRs, plus two keywords:
  private  loopback, RFC 1918, Tailscale/CGNAT 100.64.0.0/10, IPv6 ULA and
           link-local (the default)
  any      no restriction
The read-only port is never restricted: the public endpoint reaches it.
"""
from __future__ import annotations

import ipaddress

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

PRIVATE_NETS = (
    '127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
    '100.64.0.0/10',            # Tailscale (CGNAT range)
    '169.254.0.0/16',           # IPv4 link-local
    '::1/128', 'fc00::/7', 'fe80::/10',
)


def parse_allow_nets(spec: str) -> list[Network] | None:
    """'private,203.0.113.0/24' -> networks; 'any' -> None (no restriction).

    Raises ValueError on an entry that isn't a network, so a typo fails at
    startup instead of locking the admin port.
    """
    nets: list[Network] = []
    for item in (s.strip() for s in spec.split(',')):
        if not item:
            continue
        if item == 'any':
            return None
        if item == 'private':
            nets.extend(ipaddress.ip_network(n) for n in PRIVATE_NETS)
        else:
            nets.append(ipaddress.ip_network(item, strict=False))
    if not nets:
        raise ValueError('ADMIN_ALLOW_NETS is empty; use "private" or "any"')
    return nets


def client_allowed(remote_addr: str, nets: list[Network]) -> bool:
    try:
        ip = ipaddress.ip_address(remote_addr.split('%', 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in nets)
