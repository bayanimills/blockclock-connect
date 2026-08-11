"""Find BLOCKCLOCKs on the LAN.

The container reaches the LAN through the host, so probing individual IPs
works but enumerating the subnet does not - the user supplies/confirms the
subnet. We concurrently GET /api/status on every host (port 80) and keep any
that answer with the BLOCKCLOCK fingerprint (JSON containing "is_micro" /
"version"). /api/status is unmetered, so a sweep is safe for the device.

Manual IP entry (validated the same way) is always the guaranteed path.
"""

import ipaddress
import json
import logging
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("discovery")

PROBE_TIMEOUT_S = 1.8
MAX_WORKERS = 64
MAX_HOSTS = 256  # /24 at most; a bigger sweep is slow and rarely what you want


def parse_subnet(subnet):
    """'192.168.1.0/24' (or '192.168.1' / '192.168.1.0') -> list of host IPs.
    Raises ValueError with a human-readable message."""
    s = (subnet or "").strip()
    if not s:
        raise ValueError("Enter a subnet like 192.168.1.0/24")
    if "/" not in s:
        parts = s.split(".")
        if len(parts) == 3:
            s = s + ".0/24"
        else:
            s = s + "/24"
    try:
        net = ipaddress.ip_network(s, strict=False)
    except ValueError:
        raise ValueError(f"'{subnet}' is not a valid subnet "
                         "(try something like 192.168.1.0/24)")
    if net.version != 4:
        raise ValueError("Only IPv4 subnets are supported")
    if net.num_addresses > MAX_HOSTS + 2:
        raise ValueError(f"Subnet {net} is too large to scan; "
                         "use a /24 (254 hosts) or smaller")
    hosts = [str(h) for h in net.hosts()]
    if not hosts:  # /32
        hosts = [str(net.network_address)]
    return hosts


def probe(ip, timeout=PROBE_TIMEOUT_S):
    """GET http://ip/api/status; return {ip, model, version} if it looks like
    a BLOCKCLOCK, else None. Never raises."""
    try:
        req = urllib.request.Request(f"http://{ip}/api/status",
                                     headers={"User-Agent": "blockclock-connect/1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        if not isinstance(d, dict):
            return None
        if "is_micro" not in d and "version" not in d:
            return None
        return {
            "ip": ip,
            "model": "BLOCKCLOCK micro" if d.get("is_micro") else "BLOCKCLOCK mini",
            "version": str(d.get("version", "?")),
        }
    except Exception:
        return None


def scan(subnet):
    """Concurrently probe every host in the subnet; also try blockclock.local.
    Returns a sorted list of {ip, model, version}."""
    hosts = parse_subnet(subnet)

    # best-effort mDNS/hostname candidate
    try:
        mdns_ip = socket.gethostbyname("blockclock.local")
        if mdns_ip not in hosts:
            hosts.append(mdns_ip)
    except OSError:
        pass

    found = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(probe, ip): ip for ip in hosts}
        for fut in as_completed(futures):
            hit = fut.result()
            if hit:
                found.append(hit)
    found.sort(key=lambda c: tuple(int(p) for p in c["ip"].split(".")))
    log.info("scan of %s: %d hosts probed, %d clock(s) found",
             subnet, len(hosts), len(found))
    return found


def suggested_subnet():
    """Best guess at the user's LAN subnet. Inside a bridge-network container
    our own address is a docker subnet, which is useless to scan - fall back
    to the most common home LAN in that case; the field is editable anyway."""
    ip = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))  # no packets sent; just picks a route
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    if ip:
        try:
            addr = ipaddress.ip_address(ip)
            docker_ish = (addr in ipaddress.ip_network("172.16.0.0/12")
                          or ip.startswith("10.21."))  # umbrel app net
            if not docker_ish:
                return str(ipaddress.ip_network(ip + "/24", strict=False))
        except ValueError:
            pass
    return "192.168.1.0/24"
