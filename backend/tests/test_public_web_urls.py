"""Public URLs are canonicalized without connecting to an origin."""

import pytest


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://example.org/file",
        "http://127.0.0.1/",
        "http://10.1.2.3/",
        "http://169.254.169.254/",
        "http://100.100.100.200/",
        "http://0.0.0.0/",
        "http://192.168.0.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fc00::1]/",
        "http://[fe80::1]/",
        "http://localhost/",
        "https://x.local/",
        "https://metadata.google.internal/",
        "http://localhost./",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0177.0.0.1/",
        "http://127.1/",
        "http://%31%32%37.0.0.1/",
        "http://\uff11\uff12\uff17.\uff10.\uff10.\uff11/",  # Fullwidth loopback via IDNA.
        "https://user:password@example.org/",
        "https://example.org/#part",
        "https://example.org:8080/",
        "https://example.org:bad/",
        "https://example.org/?api_key=private",
        "https://example.org/?X-Amz-Signature=private",
        "https://example.org/?access%5Ftoken=private",
        "https://example.org/?code=private",
        "https://example.org/\\evil",
        "https://example.org/%0d%0aInjected",
        "https://example.org/\nsecret",
        "https://example.org@127.0.0.1/",
        "https://example.org/?q=user%40company.test",
    ],
)
def test_unsafe_public_urls_are_rejected(url: str) -> None:
    from app.modules.retrieval.domain.public_urls import validate_public_url

    with pytest.raises(ValueError, match="UNSAFE_PUBLIC_URL"):
        validate_public_url(url)


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("HTTPS://EXAMPLE.ORG:443", "https://example.org/"),
        ("https://bücher.de/path?q=planning", "https://xn--bcher-kva.de/path?q=planning"),
        ("https://example.org./docs", "https://example.org/docs"),
        ("https://[2606:4700:4700::1111]/", "https://[2606:4700:4700::1111]/"),
    ],
)
def test_public_urls_normalize_idna_case_ports_and_empty_paths(value: str, canonical: str) -> None:
    from app.modules.retrieval.domain.public_urls import validate_public_url

    assert validate_public_url(value).value == canonical


@pytest.mark.parametrize("addresses", [(), ("127.0.0.1",), ("93.184.216.34", "10.0.0.1")])
def test_dns_requires_every_answer_to_be_public(addresses: tuple[str, ...]) -> None:
    from app.modules.retrieval.domain.public_urls import validate_public_addresses

    with pytest.raises(ValueError, match="UNSAFE_PUBLIC_URL"):
        validate_public_addresses(addresses)


@pytest.mark.parametrize(
    "url",
    [
        "http://168.63.129.16/",
        "http://[2002:7f00:1::]/",
        "http://[64:ff9b::7f00:1]/",
        "https://example.org/?q=user%2540company.test",
    ],
)
def test_cloud_metadata_and_encoded_private_query_are_rejected(url: str) -> None:
    from app.modules.retrieval.domain.public_urls import validate_public_url

    with pytest.raises(ValueError, match="UNSAFE_PUBLIC_URL"):
        validate_public_url(url)
