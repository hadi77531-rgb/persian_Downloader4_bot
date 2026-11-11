from dataclasses import dataclass
import asyncio
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    platform: str


# Registrable domains only: exact match or any subdomain of these names is
# accepted. Lookalikes such as "youtube.com.evil.example" or "evil-youtube.com"
# do not match because the suffix comparison is anchored on "." + domain.
_BASE_DOMAINS = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "instagram.com": "Instagram",
    "pinterest.com": "Pinterest",
    "pin.it": "Pinterest",
}


def _detect_platform(host: str) -> str | None:
    for domain, platform in _BASE_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return platform
    return None


def validate_and_detect_url(raw: str) -> ValidatedURL:
    """Validate syntax and allow-listed registrable domain; no network request."""
    candidate = raw.strip()
    if len(candidate) > 2048 or not candidate:
        raise ValueError("لینک خالی یا بیش از حد طولانی است.")
    if any(ch.isspace() for ch in candidate):
        raise ValueError("لینک معتبر نیست.")
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("ساختار لینک معتبر نیست.") from exc
    if parsed.scheme.lower() != "https":
        raise ValueError("فقط لینک امن HTTPS پذیرفته می‌شود.")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("لینک معتبر نیست.")
    if parsed.port not in (None, 443):
        raise ValueError("درگاه لینک پذیرفته نیست.")
    host = parsed.hostname.rstrip(".").lower()
    # Reject bracketless IPv6 or raw IP literals: only named hosts are allowed.
    if host.startswith("[") or host.replace(".", "").isdigit():
        raise ValueError("این دامنه پشتیبانی نمی‌شود. لینک YouTube، Instagram یا Pinterest بفرستید.")
    platform = _detect_platform(host)
    if platform is None:
        raise ValueError("این دامنه پشتیبانی نمی‌شود. لینک YouTube، Instagram یا Pinterest بفرستید.")
    return ValidatedURL(candidate, platform)


def _resolve_public(host: str) -> None:
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("دامنه قابل دسترسی نیست.") from exc
    if not records:
        raise ValueError("دامنه به نشانی معتبر resolve نشد.")
    for record in records:
        address = ipaddress.ip_address(record[4][0].split("%", 1)[0])
        if not address.is_global:
            raise ValueError("نشانی شبکهٔ مقصد عمومی نیست.")


async def validate_public_destination(url: str) -> ValidatedURL:
    """DNS preflight; this is defense-in-depth, not a complete egress firewall."""
    validated = validate_and_detect_url(url)
    host = urlsplit(validated.url).hostname
    await asyncio.to_thread(_resolve_public, host)
    return validated


def host_matches_domain(host: str, base_domain: str) -> bool:
    """Anchored suffix match: exact domain or any subdomain of it."""
    host = host.rstrip(".").lower()
    base = base_domain.lower()
    return host == base or host.endswith("." + base)


async def ensure_public_host(host: str) -> None:
    """DNS preflight for a single host (e.g. media CDN names from an API response)."""
    if not host:
        raise ValueError("هاست مقصد نامعتبر است.")
    await asyncio.to_thread(_resolve_public, host)


def ensure_child_path(root: str, candidate: str) -> str:
    """Resolve a generated output and ensure it is contained in the job root."""
    resolved_root = Path(root).resolve()
    resolved_candidate = Path(candidate).resolve()
    if resolved_root not in resolved_candidate.parents:
        raise ValueError("مسیر خروجی خارج از پوشهٔ موقت است.")
    return str(resolved_candidate)
