from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Cloud metadata addresses, loopback, and local ranges to block explicitly
BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata.google.internal",
    "169.254.169.254",  # AWS/GCP/Azure link-local metadata
    "instance-data",
}


def is_ssrf_safe_url(url: str) -> tuple[bool, str]:
    """Validate that a URL does NOT resolve to private, loopback, or link-local cloud metadata IPs.

    Returns:
        tuple[bool, str]: (is_safe, reason_if_unsafe)
    """
    if not url or not url.strip():
        return False, "Empty URL"

    try:
        parsed = urlsplit(url)
    except Exception as exc:
        return False, f"Invalid URL formatting: {exc}"

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"Unsupported URL scheme: {scheme} (only http and https are permitted)"

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False, "Missing hostname in URL"

    # 1. Block explicitly known dangerous hostnames
    if hostname in BLOCKED_HOSTNAMES or hostname.endswith(".localhost"):
        return False, f"Access to '{hostname}' is blocked for security (SSRF prevention)"

    # 2. Check if hostname is an IP literal
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private:
            return False, f"Direct access to private IP '{ip}' is blocked (SSRF prevention)"
        if ip.is_loopback:
            return False, f"Direct access to loopback IP '{ip}' is blocked (SSRF prevention)"
        if ip.is_link_local:
            return False, f"Direct access to link-local/cloud metadata IP '{ip}' is blocked (SSRF prevention)"
        if ip.is_reserved or ip.is_multicast:
            return False, f"Direct access to reserved/multicast IP '{ip}' is blocked (SSRF prevention)"
        return True, ""
    except ValueError:
        # Hostname is a domain name, proceed to DNS resolution
        pass

    # 3. DNS resolution pre-flight check
    try:
        addr_info = socket.getaddrinfo(hostname, None)
        for _, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            ip = ipaddress.ip_address(ip_str)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False, f"Host '{hostname}' resolves to restricted IP '{ip_str}' (SSRF prevention)"
    except socket.gaierror as err:
        return False, f"DNS resolution failed for '{hostname}': {err}"
    except Exception as exc:
        return False, f"Error validating host '{hostname}': {exc}"

    return True, ""
