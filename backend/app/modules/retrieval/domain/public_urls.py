"""Pure public-URL validation. No origin fetch, DNS lookup or provider dependency."""

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NUMERIC_HOST = re.compile(r"^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*$")
_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|secret|token|apikey|authorization|credential|signature|session|cookie)"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")
_SECRET = re.compile(
    r"(?:\bsk-[a-zA-Z0-9_-]{8,}|\bBearer\s+\S+|"
    r"(?:api[_-]?key|password|secret|access[_-]?token)\s*[:=]|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\beyJ[\w-]+\.[\w-]+\.[\w-]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ValidatedPublicURL:
    value: str
    hostname: str


def _unsafe() -> ValueError:
    return ValueError("UNSAFE_PUBLIC_URL")


def _public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if value == "168.63.129.16":  # Azure virtual platform/metadata address.
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.is_global and _public_ip(str(address.ipv4_mapped))
        if address.sixtofour is not None and not _public_ip(str(address.sixtofour)):
            return False
        if address.teredo is not None or address in ipaddress.ip_network("64:ff9b::/96"):
            return False
    return address.is_global and not address.is_multicast and not address.is_unspecified


def validate_public_addresses(addresses: tuple[str, ...]) -> None:
    if not addresses or any(not _public_ip(address) for address in addresses):
        raise _unsafe()


def validate_public_url(value: str) -> ValidatedPublicURL:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 2048
        or "\\" in value
        or "#" in value
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise _unsafe()
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443 if parsed.scheme == "https" else 80}
            or "%" in parsed.hostname
        ):
            raise _unsafe()
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            labels = hostname.split(".")
            if (
                len(hostname) > 253
                or len(labels) < 2
                or labels[-1].isdigit()
                or any(_LABEL.fullmatch(label) is None for label in labels)
                or _NUMERIC_HOST.fullmatch(hostname)
                or hostname.endswith((".local", ".internal", ".localhost", ".test", ".invalid"))
            ):
                raise _unsafe() from None
        else:
            if not _public_ip(hostname):
                raise _unsafe()
        decoded = unquote(unquote(parsed.path + "?" + parsed.query))
        if any(ord(char) < 32 or ord(char) == 127 for char in decoded):
            raise _unsafe()
        for key, item in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=20):
            normalized = re.sub(r"[-_.]", "", key).casefold()
            if (
                _SENSITIVE_KEY.search(normalized)
                or normalized in {"code", "sig", "auth"}
                or normalized.startswith(("xamz", "xgoog"))
                or _EMAIL.search(unquote(unquote(item)))
                or _SECRET.search(unquote(unquote(item)))
            ):
                raise _unsafe()
        authority = f"[{hostname}]" if ":" in hostname else hostname
        canonical = urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
        return ValidatedPublicURL(canonical, hostname)
    except (ValueError, UnicodeError):
        raise _unsafe() from None


def validate_public_query(value: str) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value.strip()) <= 500
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or _SECRET.search(value)
        or _EMAIL.search(value)
    ):
        raise ValueError("UNSAFE_PUBLIC_QUERY")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("UNSAFE_PUBLIC_QUERY") from None
    for match in re.findall(r"https?://\S+", value):
        try:
            validate_public_url(match)
        except ValueError:
            raise ValueError("UNSAFE_PUBLIC_QUERY") from None
    return value.strip()
