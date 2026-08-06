#!/usr/bin/env python3
"""Detect and repair broken Sigma source parameters.

A control — in a **workbook or a data model** — can bind to a control defined
inside another data model. That binding is a "source parameter", and in the spec
it looks like this:

    - kind: control
      controlId: RegionControl          # this document's control
      id: aBcDeFgHiJcon
      parameters:                       # <-- source parameters
        - kind: data-model
          dataModelId: 11111111-1111-1111-1111-111111111111
          controlId: Store-Region       # the control inside that data model

`swapSources` remaps columns and metrics, but not parameters. After you re-point
a document at a different data model, every `parameters[].dataModelId` still
names the *old* model and every source parameter goes invalid.

This tool finds those stale bindings and rewrites them to the data model the
document actually reads from now.

What it will not do is predict whether Sigma considers a given control a valid
target. Validity depends on filter reachability through the model's join graph,
which the code representation does not expose — so Sigma is the oracle. The tool
resolves what it can prove, asks Sigma to accept the result, and translates a
rejection into something actionable.

Stdlib only — no dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

__version__ = "2.1.0"

# Exit codes, chosen so `check` works as a CI gate.
EXIT_OK = 0
EXIT_FINDINGS = 1  # stale bindings found, or the write was refused
EXIT_CONFIG = 2  # missing credentials or bad arguments
EXIT_API = 3  # the Sigma API returned an error

WORKBOOK = "workbook"
DATA_MODEL = "data model"


# --------------------------------------------------------------------------
# Spec analysis — pure functions, no network. These are the tested core.
# --------------------------------------------------------------------------

# Keys under which a container element may nest its children.
_CHILD_KEYS = ("elements", "children")


def iter_elements(elements: Iterable[dict] | None) -> Iterator[dict]:
    """Yield every element in a spec tree, descending into containers.

    Controls are frequently nested inside container elements, so a flat scan of
    ``page["elements"]`` misses them.
    """
    for element in elements or []:
        if not isinstance(element, dict):
            continue
        yield element
        for key in _CHILD_KEYS:
            child = element.get(key)
            if isinstance(child, list):
                yield from iter_elements(child)


@dataclass
class SourceParameter:
    """One `parameters[]` entry on one control element."""

    element_id: str
    element_name: str
    own_control_id: str
    data_model_id: str
    dm_control_id: str
    raw: dict = field(repr=False, compare=False)
    """The live dict inside the spec — mutate it to apply a repair in place."""
    source_dm_element_id: str | None = None
    """The data-model element this control reads its values from, if known.

    Not used to judge validity — only to suggest candidates once Sigma has
    rejected a binding.
    """


def element_source_map(spec: dict) -> dict[str, str]:
    """Element id -> the data-model element it reads. Works for either doc type."""
    sources = {}
    for page in spec.get("pages") or []:
        for element in iter_elements(page.get("elements")):
            source = element.get("source") or {}
            if (source.get("kind") == "data-model" and element.get("id")
                    and source.get("elementId")):
                sources[element["id"]] = source["elementId"]
    return sources


def _control_value_source(control: dict) -> str | None:
    """The element a control reads its values from.

    Only `source` counts. A control's `filters` may name a different element —
    that is what it filters, not where its values come from.
    """
    source = control.get("source") or {}
    inner = source.get("source") or {}
    return inner.get("elementId")


def find_source_parameters(spec: dict) -> list[SourceParameter]:
    """Collect every data-model source parameter in a workbook or model spec."""
    sources = element_source_map(spec)
    found: list[SourceParameter] = []
    for page in spec.get("pages") or []:
        for element in iter_elements(page.get("elements")):
            if element.get("kind") != "control":
                continue
            value_source = _control_value_source(element)
            for param in element.get("parameters") or []:
                if not isinstance(param, dict) or param.get("kind") != "data-model":
                    continue
                name = (element.get("name") or element.get("controlId") or "").strip()
                found.append(
                    SourceParameter(
                        element_id=element.get("id", ""),
                        element_name=name,
                        own_control_id=element.get("controlId", ""),
                        data_model_id=param.get("dataModelId", ""),
                        dm_control_id=param.get("controlId", ""),
                        raw=param,
                        source_dm_element_id=sources.get(value_source),
                    )
                )
    return found


def data_model_control_targets(dm_spec: dict) -> dict[str, str | None]:
    """Control id -> the data-model element that control filters."""
    targets: dict[str, str | None] = {}
    for page in dm_spec.get("pages") or []:
        for element in iter_elements(page.get("elements")):
            if element.get("kind") != "control" or not element.get("controlId"):
                continue
            target = None
            for filt in element.get("filters") or []:
                target = (filt.get("source") or {}).get("elementId")
                if target:
                    break
            if target is None:
                target = _control_value_source(element)
            targets[element["controlId"]] = target
    return targets


def data_model_control_ids(dm_spec: dict) -> set[str]:
    """The set of controlIds a data model defines."""
    return set(data_model_control_targets(dm_spec))


HEALTHY = "healthy"
REPAIRABLE = "repairable"
AMBIGUOUS = "ambiguous"
MISSING_CONTROL = "missing-control"


@dataclass
class Finding:
    parameter: SourceParameter
    status: str
    new_data_model_id: str | None = None
    new_control_id: str | None = None
    reason: str = ""
    available_control_ids: tuple[str, ...] = ()

    @property
    def needs_attention(self) -> bool:
        return self.status != HEALTHY

    def to_dict(self) -> dict:
        p = self.parameter
        return {
            "status": self.status,
            "elementId": p.element_id,
            "elementName": p.element_name,
            "ownControlId": p.own_control_id,
            "dataModelControlId": p.dm_control_id,
            "currentDataModelId": p.data_model_id,
            "newDataModelId": self.new_data_model_id,
            "newDataModelControlId": self.new_control_id,
            "reason": self.reason,
            "availableControlIds": list(self.available_control_ids),
            "readsDataModelElement": p.source_dm_element_id,
        }


def parse_control_map(pairs: Iterable[str]) -> dict[str, str]:
    """Parse ``OLD=NEW`` control-id rename pairs."""
    mapping: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(
                f"invalid mapping {pair!r} — expected OLD_CONTROL_ID=NEW_CONTROL_ID"
            )
        old, new = pair.split("=", 1)
        old, new = old.strip(), new.strip()
        if not old or not new:
            raise ValueError(f"invalid mapping {pair!r} — both sides must be non-empty")
        mapping[old] = new
    return mapping


def load_control_map_file(text: str) -> dict[str, str]:
    """Read a rename map from a JSON object or from ``OLD=NEW`` lines."""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"map file is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
        ):
            raise ValueError("map file JSON must be an object of string to string")
        return dict(raw)
    lines = [
        line.strip()
        for line in stripped.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return parse_control_map(lines)


def _normalize_controls(
    controls_by_data_model: dict[str, Any],
) -> dict[str, dict[str, str | None]]:
    """Accept either a set of control ids or a control-id -> target-element map."""
    normalized: dict[str, dict[str, str | None]] = {}
    for data_model_id, controls in (controls_by_data_model or {}).items():
        if isinstance(controls, dict):
            normalized[data_model_id] = dict(controls)
        else:
            normalized[data_model_id] = {control: None for control in controls}
    return normalized


def plan_repairs(
    parameters: list[SourceParameter],
    live_data_model_ids: list[str],
    controls_by_data_model: dict[str, Any],
    control_renames: dict[str, str] | None = None,
    preferred_data_model_id: str | None = None,
) -> list[Finding]:
    """Classify each source parameter and choose a repair target.

    Resolution is by identity only: does a live data model define a control of
    this id? Whether Sigma will *accept* that control as a target is not decided
    here — see the module docstring.
    """
    renames = control_renames or {}
    controls = _normalize_controls(controls_by_data_model)
    findings: list[Finding] = []
    all_available = tuple(
        sorted({c for dm in live_data_model_ids for c in controls.get(dm, {})})
    )

    for param in parameters:
        target_control = renames.get(param.dm_control_id, param.dm_control_id)
        was_renamed = target_control != param.dm_control_id
        current_is_live = param.data_model_id in live_data_model_ids
        current_defines_target = target_control in controls.get(param.data_model_id, {})

        if current_is_live and current_defines_target and not was_renamed:
            findings.append(Finding(param, HEALTHY, reason="points at a live source"))
            continue

        if current_is_live and current_defines_target and was_renamed:
            findings.append(
                Finding(
                    param,
                    REPAIRABLE,
                    param.data_model_id,
                    target_control,
                    f"renamed to {target_control!r} by an explicit mapping",
                )
            )
            continue

        candidates = [
            dm for dm in live_data_model_ids if target_control in controls.get(dm, {})
        ]

        if preferred_data_model_id and preferred_data_model_id in candidates:
            findings.append(
                Finding(
                    param,
                    REPAIRABLE,
                    preferred_data_model_id,
                    target_control if was_renamed else None,
                    f"--data-model selected {preferred_data_model_id}",
                )
            )
        elif len(candidates) == 1:
            note = f" (renamed from {param.dm_control_id!r})" if was_renamed else ""
            findings.append(
                Finding(
                    param,
                    REPAIRABLE,
                    candidates[0],
                    target_control if was_renamed else None,
                    f"live source defines control {target_control!r}{note}",
                )
            )
        elif len(candidates) > 1:
            findings.append(
                Finding(
                    param,
                    AMBIGUOUS,
                    reason=(
                        f"control {target_control!r} is defined by {len(candidates)} "
                        f"live sources: {', '.join(candidates)} — choose one with "
                        f"--data-model"
                    ),
                    available_control_ids=tuple(candidates),
                )
            )
        elif not live_data_model_ids:
            findings.append(
                Finding(param, MISSING_CONTROL,
                        reason="this document has no live data-model sources")
            )
        else:
            findings.append(
                Finding(
                    param,
                    MISSING_CONTROL,
                    reason=(
                        f"no live source defines control {target_control!r} — it was "
                        f"renamed or removed; map it with "
                        f"--map {param.dm_control_id}=NEW_CONTROL_ID"
                    ),
                    available_control_ids=all_available,
                )
            )
    return findings


def blocking_findings(findings: list[Finding]) -> list[Finding]:
    """Findings that make the document unwritable.

    Sigma validates an entire spec on write and rejects any invalid source
    parameter, so a *partial* repair cannot be persisted: one unresolved binding
    fails the whole write and nothing changes.
    """
    return [f for f in findings if f.status in (AMBIGUOUS, MISSING_CONTROL)]


def apply_repairs(findings: list[Finding]) -> int:
    """Rewrite repairable bindings in place. Returns the number changed."""
    changed = 0
    for finding in findings:
        if finding.status != REPAIRABLE:
            continue
        if finding.new_data_model_id:
            finding.parameter.raw["dataModelId"] = finding.new_data_model_id
        if finding.new_control_id:
            finding.parameter.raw["controlId"] = finding.new_control_id
        changed += 1
    return changed


# Read-only fields the API returns but will not accept back on a write.
# A denylist, deliberately: anything else in the response is document content and
# must survive the round trip, including fields this tool has never heard of.
ENVELOPE_METADATA = frozenset({
    "workbookId", "dataModelId", "name", "url", "description",
    "documentVersion", "latestDocumentVersion", "ownerId", "folderId",
    "createdBy", "updatedBy", "createdAt", "updatedAt",
})


def unwrap_document(envelope: dict) -> tuple[dict, bool]:
    """Split a GET response into (writable document, was it wrapped).

    Workbooks nest the document under a `document` key; data models return it
    flat alongside read-only metadata. Returns the live inner dict — mutating
    its `pages` in place is reflected in what gets written back.
    """
    document = envelope.get("document")
    if isinstance(document, dict):
        return document, True
    return {k: v for k, v in envelope.items() if k not in ENVELOPE_METADATA}, False


def document_for_write(document: dict, wrapped: bool) -> dict:
    """Wrap the document for the write endpoint, unchanged.

    The document is sent back **whole**. Cherry-picking known fields is how
    layout gets lost: a body that omits `layout` is accepted and Sigma
    regenerates it, which moves every element on the page. `kind` is likewise
    required, and was not a field this tool originally knew about.
    """
    return {"document": document} if wrapped else dict(document)


# --------------------------------------------------------------------------
# Translating Sigma's rejection into something actionable
# --------------------------------------------------------------------------

INVALID_PARAMETER_RE = re.compile(
    r"Invalid parameter on control:\s*(?P<element>[^\s]+)\s+"
    r"targeting data model:\s*(?P<data_model>[^,]+),\s*"
    r"controlId:\s*(?P<control>.+?)\.?$",
    re.MULTILINE,
)


def parse_invalid_parameter(message: str) -> dict | None:
    """Pull the element, data model and control out of Sigma's rejection."""
    match = INVALID_PARAMETER_RE.search(message or "")
    if not match:
        return None
    return {
        "elementId": match.group("element").strip(),
        "dataModelId": match.group("data_model").strip(),
        "controlId": match.group("control").strip(),
    }


def _name_stem(control_id: str) -> str:
    """The trailing word of a control id, for ordering candidates by likeness."""
    parts = [p for p in re.split(r"[^0-9A-Za-z]+", control_id) if p]
    return parts[-1].lower() if parts else control_id.lower()


def candidates_for(
    parameter: SourceParameter | None,
    controls: dict[str, str | None],
    like: str,
) -> tuple[str, ...]:
    """Controls filtering the element this control reads, likest name first."""
    if parameter is None or parameter.source_dm_element_id is None:
        return ()
    matches = [
        control
        for control, target in controls.items()
        if target == parameter.source_dm_element_id
    ]
    stem = _name_stem(like)
    return tuple(sorted(matches, key=lambda c: (_name_stem(c) != stem, c)))


def explain_rejection(
    message: str,
    parameters: list[SourceParameter],
    controls_by_data_model: dict[str, Any],
) -> str:
    """Turn Sigma's 400 into guidance, without asserting the user is wrong."""
    parsed = parse_invalid_parameter(message)
    if not parsed:
        return message
    controls = _normalize_controls(controls_by_data_model)
    param = next(
        (p for p in parameters if p.element_id == parsed["elementId"]), None
    )
    target_control = parsed["controlId"]
    dm_controls = controls.get(parsed["dataModelId"], {})

    lines = [
        f"Sigma refused the binding on control {parsed['elementId']}:",
        f"  target: {parsed['dataModelId']} / {target_control}",
    ]
    if target_control not in dm_controls:
        lines.append(f"  that data model does not define a control named "
                     f"{target_control!r}.")
        if dm_controls:
            lines.append(f"  it defines: {', '.join(sorted(dm_controls))}")
        return "\n".join(lines)

    reads = param.source_dm_element_id if param else None
    filters = dm_controls.get(target_control)
    if reads and filters and reads != filters:
        lines.append(f"  this control reads values from element {reads}, while "
                     f"{target_control!r} filters {filters}.")
    lines.append("  The control exists, so this is Sigma's write API declining "
                 "the pairing rather than a missing id.")

    options = candidates_for(param, dm_controls, target_control)
    if options:
        lines.append("  Controls filtering the element it reads: "
                     + ", ".join(options))
        lines.append(f"  To retarget: --map {parsed['controlId']}={options[0]}")
    lines.append("  If you believe this pairing is correct, set it in the Sigma "
                 "UI — the write API will not accept it.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Sigma API client
# --------------------------------------------------------------------------


@dataclass
class Document:
    """A workbook or data model, as fetched: envelope plus writable content."""

    kind: str
    document_id: str
    envelope: dict = field(repr=False)
    content: dict = field(repr=False)
    wrapped: bool

    @property
    def name(self) -> Any:
        return self.envelope.get("name")

    @property
    def version(self) -> Any:
        return self.envelope.get("documentVersion")

    @property
    def layout(self) -> Any:
        return self.content.get("layout")


class SigmaError(RuntimeError):
    pass


class SigmaConfigError(SigmaError):
    """Bad input or credentials, as opposed to an API failure."""


class SigmaClient:
    """Minimal client for the endpoints this tool needs."""

    def __init__(self, base_url: str, client_id: str, client_secret: str):
        self.base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None

    @classmethod
    def from_env(cls) -> "SigmaClient":
        missing = [
            name
            for name in ("SIGMA_BASE_URL", "SIGMA_CLIENT_ID", "SIGMA_CLIENT_SECRET")
            if not os.environ.get(name)
        ]
        if missing:
            raise SigmaConfigError(
                "missing environment variable(s): "
                + ", ".join(missing)
                + "\nSee the README for how to obtain API credentials."
            )
        return cls(
            os.environ["SIGMA_BASE_URL"],
            os.environ["SIGMA_CLIENT_ID"],
            os.environ["SIGMA_CLIENT_SECRET"],
        )

    @property
    def token(self) -> str:
        if self._token is None:
            payload = urllib.parse.urlencode(
                {
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                }
            ).encode()
            request = urllib.request.Request(
                f"{self.base_url}/v2/auth/token",
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request) as response:
                    self._token = json.load(response)["access_token"]
            except urllib.error.HTTPError as exc:
                raise SigmaConfigError(
                    f"token exchange failed (HTTP {exc.code}). Check "
                    f"SIGMA_BASE_URL matches your cloud and region, and that the "
                    f"client id/secret are correct."
                ) from exc
            except (KeyError, json.JSONDecodeError) as exc:
                raise SigmaError("token endpoint returned an unexpected body") from exc
        return self._token

    def call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:1000]
            try:
                detail = json.loads(detail).get("message", detail)
            except json.JSONDecodeError:
                pass
            raise SigmaError(f"HTTP {exc.code} on {method} {path}\n{detail}") from exc
        except urllib.error.URLError as exc:
            raise SigmaError(f"could not reach {self.base_url}: {exc.reason}") from exc
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw  # the spec write endpoints answer in YAML

    _call = call  # kept for callers written against the old private name

    # -- document-kind aware helpers ---------------------------------------

    def detect_kind(self, document_id: str) -> str:
        """Is this id a workbook or a data model?"""
        for path, kind in (("workbooks", WORKBOOK), ("dataModels", DATA_MODEL)):
            try:
                self.call("GET", f"/v2/{path}/{document_id}")
                return kind
            except SigmaError:
                continue
        raise SigmaConfigError(
            f"{document_id!r} is neither a workbook nor a data model that these "
            f"credentials can see. Check the id and that the account has access."
        )

    def _base(self, kind: str) -> str:
        return "workbooks" if kind == WORKBOOK else "dataModels"

    def get_spec(self, kind: str, document_id: str) -> dict:
        """The raw GET response — envelope and all."""
        return self.call(
            "GET", f"/v2/{self._base(kind)}/{document_id}/spec?format=json"
        )

    def update_spec(self, kind: str, document_id: str, body: dict) -> Any:
        return self.call("PUT", f"/v2/{self._base(kind)}/{document_id}/spec", body)

    def get_document(self, kind: str, document_id: str) -> "Document":
        envelope = self.get_spec(kind, document_id)
        content, wrapped = unwrap_document(envelope)
        return Document(kind, document_id, envelope, content, wrapped)

    def write_document(self, document: "Document") -> Any:
        return self.update_spec(
            document.kind,
            document.document_id,
            document_for_write(document.content, document.wrapped),
        )

    def get_sources(self, kind: str, document_id: str) -> list[dict]:
        """Normalize the two response shapes: a bare list, or {entries: [...]}."""
        raw = self.call("GET", f"/v2/{self._base(kind)}/{document_id}/sources")
        if isinstance(raw, dict):
            raw = raw.get("entries") or []
        return raw if isinstance(raw, list) else []

    def resolve_data_model_id(self, given: str) -> str:
        """Accept a url id as well as a UUID."""
        try:
            meta = self.call("GET", f"/v2/dataModels/{given}")
        except SigmaError as exc:
            raise SigmaConfigError(
                f"--data-model {given!r} is not a data model these credentials "
                f"can see."
            ) from exc
        resolved = meta.get("dataModelId")
        if not resolved:
            raise SigmaConfigError(f"--data-model {given!r} did not resolve to an id")
        return resolved


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

_LABEL = {
    HEALTHY: "ok       ",
    REPAIRABLE: "REPAIR   ",
    AMBIGUOUS: "AMBIGUOUS",
    MISSING_CONTROL: "NO MATCH ",
}


@dataclass
class Analysis:
    document: Document
    live: list[str]
    findings: list[Finding]
    parameters: list[SourceParameter]
    controls: dict[str, dict[str, str | None]]

    @property
    def kind(self) -> str:
        return self.document.kind


def analyze(
    client: SigmaClient,
    kind: str,
    document_id: str,
    control_renames: dict[str, str] | None = None,
    preferred_data_model_id: str | None = None,
) -> Analysis:
    document = client.get_document(kind, document_id)
    sources = client.get_sources(kind, document_id)
    live = [s["dataModelId"] for s in sources if s.get("type") == "data-model"]
    parameters = find_source_parameters(document.content)
    controls = {}
    for data_model_id in live:
        model, _ = unwrap_document(client.get_spec(DATA_MODEL, data_model_id))
        controls[data_model_id] = data_model_control_targets(model)
    findings = plan_repairs(
        parameters, live, controls, control_renames, preferred_data_model_id
    )
    return Analysis(document, live, findings, parameters, controls)


def _print_report(analysis: Analysis) -> None:
    document = analysis.document
    print(f"{analysis.kind}: {document.name}  (version {document.version})")
    print(f"reads from: {', '.join(analysis.live) if analysis.live else '(none)'}")
    print(f"source parameters: {len(analysis.findings)}")
    if not analysis.findings:
        print(f"\nThis {analysis.kind} has no data-model source parameters.")
        return
    print()
    for finding in analysis.findings:
        p = finding.parameter
        print(f"  [{_LABEL[finding.status]}] {p.element_name or p.own_control_id!r}"
              f"  (element {p.element_id})")
        print(f"             data model control: {p.dm_control_id}")
        if finding.status == HEALTHY:
            print(f"             {p.data_model_id}")
        elif finding.status == REPAIRABLE:
            print(f"             {p.data_model_id}")
            print(f"          -> {finding.new_data_model_id}")
            if finding.new_control_id:
                print(f"             control {p.dm_control_id} -> "
                      f"{finding.new_control_id}")
        else:
            print(f"             {p.data_model_id}  (unresolved)")
        print(f"             {finding.reason}")
        if finding.status == MISSING_CONTROL and finding.available_control_ids:
            print(f"             controls available: "
                  f"{', '.join(finding.available_control_ids)}")
        print()


def _summary(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.status] = counts.get(finding.status, 0) + 1
    return counts


def _emit_json(document_id: str, analysis: Analysis, **extra: Any) -> None:
    print(json.dumps(
        {
            "documentKind": analysis.kind,
            "document": analysis.document.name,
            "documentId": document_id,
            "liveDataModelIds": analysis.live,
            "summary": _summary(analysis.findings),
            "findings": [f.to_dict() for f in analysis.findings],
            **extra,
        },
        indent=2,
    ))


def _mappings_from_args(args: argparse.Namespace) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if getattr(args, "map_file", None):
        try:
            with open(args.map_file, encoding="utf-8") as handle:
                mapping.update(load_control_map_file(handle.read()))
        except OSError as exc:
            raise SigmaConfigError(f"could not read --map-file: {exc}") from exc
        except ValueError as exc:
            raise SigmaConfigError(str(exc)) from exc
    try:
        mapping.update(parse_control_map(getattr(args, "map", None) or []))
    except ValueError as exc:
        raise SigmaConfigError(str(exc)) from exc
    return mapping


def _prepare(client: SigmaClient, args: argparse.Namespace) -> tuple[Analysis, dict]:
    kind = {"workbook": WORKBOOK, "datamodel": DATA_MODEL}.get(
        getattr(args, "type", None) or "", None
    ) or client.detect_kind(args.document_id)
    renames = _mappings_from_args(args)
    preferred = None
    if args.data_model:
        preferred = client.resolve_data_model_id(args.data_model)
    analysis = analyze(client, kind, args.document_id, renames, preferred)
    if preferred and preferred not in analysis.live:
        raise SigmaConfigError(
            f"--data-model {args.data_model!r} resolved to {preferred}, which is "
            f"not one of this {kind}'s live sources "
            f"({', '.join(analysis.live) or 'none'})."
        )
    return analysis, renames


def cmd_check(client: SigmaClient, args: argparse.Namespace) -> int:
    analysis, renames = _prepare(client, args)
    if args.json:
        _emit_json(args.document_id, analysis, appliedMappings=renames)
    else:
        _print_report(analysis)
    broken = [f for f in analysis.findings if f.needs_attention]
    if not args.json:
        if broken:
            print(f"{len(broken)} source parameter(s) need attention.")
        elif analysis.findings:
            print("All source parameters resolve to a live source.")
            print("Whether Sigma accepts each target is only settled by a write; "
                  "run `repair --apply` to find out.")
    return EXIT_FINDINGS if broken else EXIT_OK


def _blocked_hint(unresolved: list[Finding]) -> str:
    """Suggest only the flags that match what is actually unresolved."""
    kinds = {f.status for f in unresolved}
    parts = []
    if MISSING_CONTROL in kinds:
        parts.append("--map OLD_CONTROL_ID=NEW_CONTROL_ID")
    if AMBIGUOUS in kinds:
        parts.append("--data-model ID")
    return " or ".join(parts) if parts else "an explicit mapping"


def cmd_repair(client: SigmaClient, args: argparse.Namespace) -> int:
    analysis, renames = _prepare(client, args)
    repairable = [f for f in analysis.findings if f.status == REPAIRABLE]
    unresolved = blocking_findings(analysis.findings)

    if not args.json:
        _print_report(analysis)

    if unresolved:
        if args.json:
            _emit_json(args.document_id, analysis, applied=False, blocked=True,
                       wouldRepair=len(repairable), appliedMappings=renames)
        else:
            print(f"Cannot write: {len(unresolved)} binding(s) could not be resolved.")
            print(f"Sigma validates the whole spec on write, so a partial repair "
                  f"cannot be saved. Resolve them with {_blocked_hint(unresolved)}, "
                  f"then run again.")
            if repairable:
                print(f"\n{len(repairable)} other binding(s) are ready and will be "
                      f"written in the same pass once the rest resolve.")
        return EXIT_FINDINGS

    if not repairable:
        if args.json:
            _emit_json(args.document_id, analysis, applied=False,
                       appliedMappings=renames)
        else:
            print("Nothing to repair.")
        return EXIT_OK

    if not args.apply:
        if args.json:
            _emit_json(args.document_id, analysis, applied=False,
                       wouldRepair=len(repairable), appliedMappings=renames)
        else:
            print(f"Dry run — {len(repairable)} binding(s) would be rewritten.")
            print("Re-run with --apply to write the change.")
        return EXIT_OK

    changed = apply_repairs(analysis.findings)
    layout_before = analysis.document.layout
    try:
        # The document is written back whole, so layout and every other field
        # survive; only the bindings were mutated, in place.
        client.write_document(analysis.document)
    except SigmaError as exc:
        explanation = explain_rejection(
            str(exc), analysis.parameters, analysis.controls
        )
        print(f"\nThe write was refused, so nothing changed.\n", file=sys.stderr)
        print(explanation, file=sys.stderr)
        return EXIT_FINDINGS

    after, _ = _prepare(client, args)
    still_broken = [f for f in after.findings if f.needs_attention]
    layout_kept = after.document.layout == layout_before
    if args.json:
        _emit_json(args.document_id, after, applied=True, repaired=changed,
                   stillNeedingAttention=len(still_broken),
                   layoutUnchanged=layout_kept, appliedMappings=renames)
    else:
        print(f"Applied — rewrote {changed} binding(s). A new version was created; "
              f"the previous one is still available in Sigma.")
        print(f"Verified — {len(still_broken)} source parameter(s) still need "
              f"attention.")
        if layout_kept:
            print("Verified — the layout is byte-identical; nothing moved.")
        else:
            print("WARNING — the layout changed. Elements may have moved; the "
                  "previous version is still available in Sigma.", file=sys.stderr)
    return EXIT_FINDINGS if still_broken else EXIT_OK


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("document_id",
                        help="workbook or data model id (UUID or url id)")
    parser.add_argument(
        "--type", choices=["workbook", "datamodel"],
        help="skip auto-detection of the document type",
    )
    parser.add_argument(
        "--map", action="append", metavar="OLD=NEW",
        help="rename a data-model control id, for controls renamed between "
             "template and clone. Repeatable.",
    )
    parser.add_argument(
        "--map-file", metavar="PATH",
        help="read control-id renames from a JSON object or OLD=NEW lines",
    )
    parser.add_argument(
        "--data-model", metavar="ID",
        help="prefer this data model when several live sources define the same "
             "control id (UUID or url id)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigma-source-params",
        description="Detect and repair broken Sigma source parameters in "
                    "workbooks and data models.",
        epilog="Credentials come from SIGMA_BASE_URL, SIGMA_CLIENT_ID and "
               "SIGMA_CLIENT_SECRET.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser(
        "check", help="report stale source parameters; exits 1 if any are found")
    _add_common_flags(check)
    check.set_defaults(func=cmd_check)

    repair = subparsers.add_parser(
        "repair", help="rewrite stale source parameters (dry run unless --apply)")
    _add_common_flags(repair)
    repair.add_argument(
        "--apply", action="store_true",
        help="write the change (creates a new version)",
    )
    repair.set_defaults(func=cmd_repair)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = SigmaClient.from_env()
        return args.func(client, args)
    except SigmaConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except SigmaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_API
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
