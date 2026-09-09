"""TLS posture of the FHIR client.

A local HAPI server is plain HTTP, so constructing a client for it must not
depend on the machine's CA trust store -- httpx builds an SSL context eagerly,
regardless of scheme, which turns an unrelated trust-store problem into a
"cannot reach the local FHIR server" failure.

Verification stays fully enabled for https, which is what the Epic adapter will
use (FHIR.md).
"""

from __future__ import annotations

import ssl
from pathlib import Path

import certifi
import httpx
import pytest

from app.fhir.client import FhirClient, uses_tls


@pytest.fixture
def unusable_ca_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A CA bundle that exists but contains no certificates.

    This is the real-world failure: ssl raises
    ``[X509: NO_CERTIFICATE_OR_CRL_FOUND]``, not FileNotFoundError.
    """
    bundle = tmp_path / "cacert.pem"
    bundle.write_text("# no certificates here\n")
    # httpx imports certifi inside create_ssl_context, so patching the module
    # attribute is what the call actually resolves.
    monkeypatch.setattr(certifi, "where", lambda: str(bundle))
    return bundle


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://localhost:8080/fhir", False),
        ("http://hapi-fhir:8080/fhir", False),
        ("https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4", True),
        ("https://example.org/fhir", True),
    ],
)
def test_tls_is_enabled_only_for_https(base_url: str, expected: bool) -> None:
    assert uses_tls(base_url) is expected


def test_the_broken_trust_store_really_does_break_the_default_client(
    unusable_ca_bundle: Path,
) -> None:
    """Guards the fixture itself: if this stops failing, the test below proves nothing."""
    with pytest.raises(ssl.SSLError):
        httpx.AsyncClient(base_url="http://localhost:8080/fhir")


def test_local_client_constructs_without_a_usable_trust_store(
    unusable_ca_bundle: Path,
) -> None:
    client = FhirClient("http://localhost:8080/fhir")
    assert client.base_url == "http://localhost:8080/fhir"


def test_https_client_still_requires_certificate_verification(
    unusable_ca_bundle: Path,
) -> None:
    """The workaround must never weaken a real TLS connection."""
    with pytest.raises(ssl.SSLError):
        FhirClient("https://fhir.epic.com/api")


def test_trailing_slash_is_normalised() -> None:
    assert FhirClient("http://localhost:8080/fhir/").base_url == "http://localhost:8080/fhir"
