"""Client IP handling.

The address stored on an entry is the direct connection, with the port removed.
X-Forwarded-For and X-Real-IP are used only when that address is listed in
CAPLOG_TRUSTED_PROXIES. Otherwise a client could put any address in those headers.
"""

import ipaddress


def parse_trusted_proxies(text: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets = []
    for raw in text.split(","):
        part = raw.strip()
        if not part:
            continue
        try:
            if "/" not in part:
                ip = ipaddress.ip_address(part)
                part = f"{ip}/{32 if ip.version == 4 else 128}"
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError as exc:
            raise ValueError(f"trusted proxy {raw.strip()!r} is not a valid IP or CIDR") from exc
    return nets


def _in_nets(host: str, nets: list) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in nets)


def client_ip(remote_host: str, x_forwarded_for: str, x_real_ip: str, trusted: list) -> str:
    host = remote_host or ""
    if not _in_nets(host, trusted):
        return host
    if x_forwarded_for:
        first = x_forwarded_for.split(",", 1)[0].strip()
        if first:
            return first
    real = x_real_ip.strip()
    if real:
        return real
    return host
