from urllib.parse import urlsplit, urlunsplit

from w3lib.url import canonicalize_url


def normalize_url(url: str) -> str | None:
    """Resolve and canonicalize an HTTP(S) URL, dropping fragments."""
    if not url:
        return None

    normalized = canonicalize_url(url, keep_blank_values=True)
    parts = urlsplit(normalized)
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()

    if scheme not in {"http", "https"} or not hostname:
        return None

    try:
        port = parts.port
    except ValueError:
        return None

    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"
    if parts.username or parts.password:
        return None

    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def host_is_allowed(url: str, allowed_domains: list[str]) -> bool:
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(
        hostname == domain.lower().rstrip(".") or hostname.endswith(f".{domain.lower().rstrip('.')}")
        for domain in allowed_domains
    )
