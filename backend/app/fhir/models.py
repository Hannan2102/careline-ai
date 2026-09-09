"""Lightweight FHIR helpers.

Deliberately not a full FHIR object model. Resources are handled as JSON
dictionaries and converted to domain models immediately (fhir/mappings.py);
a heavyweight resource library would buy little here and would encourage FHIR
types to leak upward, which ADR 001 forbids.
"""

from __future__ import annotations

from typing import Any, TypeAlias

FhirResource: TypeAlias = dict[str, Any]
FhirBundle: TypeAlias = dict[str, Any]


def reference(resource: FhirResource) -> str:
    """'Patient/demo-john-smith' from a resource dict."""
    return f"{resource['resourceType']}/{resource['id']}"


def resource_id(ref: str) -> str:
    """'demo-john-smith' from 'Patient/demo-john-smith'."""
    return ref.rsplit("/", 1)[-1]


def get_path(resource: FhirResource, *path: str | int, default: Any = None) -> Any:
    """Safely walk a nested resource path.

    FHIR elements are almost all optional, so absence is normal and must not
    raise. ``get_path(mr, "dosageInstruction", 0, "text")`` returns ``default``
    if any step is missing.
    """
    current: Any = resource
    for key in path:
        if isinstance(key, int):
            if not isinstance(current, list) or len(current) <= key:
                return default
            current = current[key]
        else:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]
    return current if current is not None else default


def make_bundle(resources: list[FhirResource], bundle_type: str = "transaction") -> FhirBundle:
    """Wrap resources in a bundle using conditional PUT so seeding is idempotent."""
    return {
        "resourceType": "Bundle",
        "type": bundle_type,
        "entry": [
            {
                "fullUrl": f"urn:uuid:{r['id']}",
                "resource": r,
                "request": {"method": "PUT", "url": reference(r)},
            }
            for r in resources
        ],
    }


def bundle_resources(bundle: FhirBundle) -> list[FhirResource]:
    """Resources out of a search-set bundle, tolerating an empty result."""
    return [
        entry["resource"]
        for entry in bundle.get("entry", []) or []
        if isinstance(entry, dict) and "resource" in entry
    ]
