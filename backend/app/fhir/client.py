"""Async FHIR R4 REST client.

A thin, honest wrapper over httpx: it speaks FHIR HTTP semantics (search
bundles, conditional updates, ``If-Match`` version checks) and translates
transport failures into the EHR error taxonomy. It holds no business logic --
that lives in the provider above it.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self
from urllib.parse import urlparse

import httpx

from app.ehr.base import EHRConflictError, EHRNotFoundError, EHRUnavailableError
from app.fhir.models import FhirBundle, FhirResource, bundle_resources

FHIR_JSON = "application/fhir+json"


class FhirClient:
    """HTTP access to a FHIR R4 server."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Accept": FHIR_JSON, "Content-Type": FHIR_JSON},
            verify=uses_tls(self.base_url),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------ requests
    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise EHRUnavailableError(f"FHIR request failed: {method} {url}: {exc}") from exc

        if response.status_code == 404:
            raise EHRNotFoundError(f"not found: {method} {url}")
        if response.status_code in (409, 412):
            # 412 is what a failed If-Match looks like: someone else won.
            raise EHRConflictError(f"conflict on {method} {url}: {response.status_code}")
        if response.status_code >= 400:
            raise EHRUnavailableError(
                f"FHIR error {response.status_code} on {method} {url}: {response.text[:300]}"
            )
        return response

    async def capability_statement(self) -> FhirResource:
        """``GET /metadata`` -- the standard FHIR reachability check."""
        response = await self._request("GET", "/metadata")
        return dict(response.json())

    async def ping(self) -> bool:
        """True when the server answers ``/metadata``. Never raises."""
        try:
            statement = await self.capability_statement()
        except Exception:
            return False
        return statement.get("resourceType") == "CapabilityStatement"

    async def read(self, resource_type: str, resource_id: str) -> FhirResource:
        response = await self._request("GET", f"/{resource_type}/{resource_id}")
        return dict(response.json())

    async def try_read(self, resource_type: str, resource_id: str) -> FhirResource | None:
        try:
            return await self.read(resource_type, resource_id)
        except EHRNotFoundError:
            return None

    async def search(
        self, resource_type: str, params: dict[str, Any], page_limit: int = 5
    ) -> list[FhirResource]:
        """Search, following ``next`` links up to ``page_limit`` pages."""
        response = await self._request("GET", f"/{resource_type}", params=params)
        bundle: FhirBundle = response.json()
        resources = bundle_resources(bundle)

        pages = 1
        while pages < page_limit:
            next_url = next(
                (
                    link.get("url")
                    for link in bundle.get("link", [])
                    if link.get("relation") == "next"
                ),
                None,
            )
            if not next_url:
                break
            response = await self._request("GET", next_url)
            bundle = response.json()
            resources.extend(bundle_resources(bundle))
            pages += 1
        return resources

    async def create(self, resource: FhirResource) -> FhirResource:
        """POST, letting the server assign the id."""
        response = await self._request("POST", f"/{resource['resourceType']}", json=resource)
        return dict(response.json()) if response.content else resource

    async def update(self, resource: FhirResource, if_match: str | None = None) -> FhirResource:
        """PUT at a known id.

        Pass ``if_match`` (a version id) to make the write conditional -- this
        is how slot booking avoids a lost update; the loser gets a 412 and a
        typed :class:`EHRConflictError`.
        """
        headers = {"If-Match": f'W/"{if_match}"'} if if_match else None
        response = await self._request(
            "PUT",
            f"/{resource['resourceType']}/{resource['id']}",
            json=resource,
            headers=headers,
        )
        return dict(response.json()) if response.content else resource

    async def delete(self, resource_type: str, resource_id: str) -> None:
        """Delete one resource. A missing resource is not an error."""
        try:
            await self._request("DELETE", f"/{resource_type}/{resource_id}")
        except EHRNotFoundError:
            return

    async def delete_matching(self, resource_type: str, params: dict[str, Any]) -> None:
        """Conditional delete of every match.

        Requires the server to permit multiple delete (enabled in
        infra/hapi/application.yaml). Used to reset demo state between runs.
        """
        try:
            await self._request("DELETE", f"/{resource_type}", params=params)
        except EHRNotFoundError:
            return

    async def transaction(self, bundle: FhirBundle) -> FhirBundle:
        """Submit a transaction bundle to the server root."""
        response = await self._request("POST", "/", json=bundle)
        return dict(response.json()) if response.content else {}


def uses_tls(base_url: str) -> bool:
    """Whether this base URL will actually negotiate TLS.

    httpx builds an SSL context when the client is constructed, regardless of
    scheme, so a plain-HTTP connection to a local HAPI server would otherwise
    fail on a machine whose CA trust store is broken -- a failure that has
    nothing to do with the request being made.

    Returning False for http:// skips loading a trust store that would never be
    used. Certificate verification stays fully enabled for https://, which is
    what the Epic adapter will use.
    """
    return urlparse(base_url).scheme == "https"


def version_id(resource: FhirResource) -> str | None:
    """``meta.versionId``, used for conditional updates."""
    meta = resource.get("meta") or {}
    version = meta.get("versionId")
    return str(version) if version is not None else None
