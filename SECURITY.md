# Security Policy

## Supported Versions

Use this section to tell people about which versions of your project are
currently being supported with security updates.

| Version | Supported          |
| ------- | ------------------ |
| 5.1.x   | :white_check_mark: |
| 5.0.x   | :x:                |
| 4.0.x   | :white_check_mark: |
| < 4.0   | :x:                |

## Reporting a Vulnerability

Use this section to tell people how to report a vulnerability.

Tell them where to go, how often they can expect to get an update on a
reported vulnerability, what to expect if the vulnerability is accepted or
declined, etc.

## Privileges

The monitor manages root-owned services and config files. Run it as the dedicated
`adsb-monitor` user (`sudo installer/service-user.sh enable`, see README):
- **The process:** the Flask process runs unprivileged.
- **The boundary:** root actions go through `helper.py` and the allow-list in
  `system/privileged.py` (paths, file contents, unit names, container, component).
- **What remains:** someone with the admin password can stop or restart services
  and change decoder options. They can't get a root shell through the dashboard.
- **What the root helper needs:** it imports the code in `/opt/adsb-monitor`, so that
  tree must stay root-owned and not writable by others. `service-user.sh` checks
  this.
- **Logging:** the helper logs each request that changes something to the auth log.
