#!/usr/bin/env python3
"""Detect and repair broken Sigma workbook source parameters.

A Sigma workbook control can bind to a control defined inside a data model — a
"source parameter". In the workbook spec that binding looks like this:

    - kind: control
      controlId: RegionControl          # the workbook-side control
      id: aBcDeFgHiJcon
      parameters:                       # <-- source parameters
        - kind: data-model
          dataModelId: 11111111-1111-1111-1111-111111111111
          controlId: Store-Region       # the control inside the data model

`swapSources` remaps columns and metrics, but it does not remap parameters. After
you re-point a workbook at a different data model, every `parameters[].dataModelId`
still names the *old* model and every source parameter goes invalid.

This tool finds those stale bindings and rewrites them to the data model the
workbook actually reads from now.

Stdlib only — no dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

__version__ = "1.0.0"

# Exit codes, chosen so `check` works as a CI gate.
EXIT_OK = 0
EXIT_FINDINGS = 1  # stale bindings found (check), or some were unresolvable
EXIT_CONFIG = 2  # missing credentials or bad arguments
EXIT_API = 3  # the Sigma API returned an error


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
    workbook_control_id: str
    data_model_id: str
    dm_control_id: str
    raw: dict = field(repr=False, compare=False)
    """The live dict inside the spec — mutate it to apply a repair in place."""


def find_source_parameters(spec: dict) -> list[SourceParameter]:
    """Collect every data-model source parameter in a workbook spec."""
    found: list[SourceParameter] = []
    for page in spec.get("pages") or []:
        for element in iter_elements(page.get("elements")):
            if element.get("kind") != "control":
                continue
            for param in element.get("parameters") or []:
                if not isinstance(param, dict) or param.get("kind") != "data-model":
                    continue
                name = (element.get("name") or element.get("controlId") or "").strip()
                found.append(
                    SourceParameter(
                        element_id=element.get("id", ""),
                        element_name=name,
                        workbook_control_id=element.get("controlId", ""),
                        data_model_id=param.get("dataModelId", ""),
                        dm_control_id=param.get("controlId", ""),
                        raw=param,
                    )
                )
    return found


def data_model_control_ids(dm_spec: dict) -> set[str]:
    """The set of controlIds a data model defines."""
    ids = set()
    for page in dm_spec.get("pages") or []:
        for element in iter_elements(page.get("elements")):
            if element.get("kind") == "control" and element.get("controlId"):
                ids.add(element["controlId"])
    return ids


HEALTHY = "healthy"
REPAIRABLE = "repairable"
AMBIGUOUS = "ambiguous"
MISSING_CONTROL = "missing-control"


@dataclass
class Finding:
    parameter: SourceParameter
    status: str
    new_data_model_id: str | None = None
    reason: str = ""

    @property
    def needs_attention(self) -> bool:
        return self.status != HEALTHY

    def to_dict(self) -> dict:
        p = self.parameter
        return {
            "status": self.status,
            "elementId": p.element_id,
            "elementName": p.element_name,
            "workbookControlId": p.workbook_control_id,
            "dataModelControlId": p.dm_control_id,
            "currentDataModelId": p.data_model_id,
            "newDataModelId": self.new_data_model_id,
            "reason": self.reason,
        }


def plan_repairs(
    parameters: list[SourceParameter],
    live_data_model_ids: list[str],
    controls_by_data_model: dict[str, set[str]],
) -> list[Finding]:
    """Classify each source parameter and choose a repair target.

    A parameter already pointing at a live source is ``HEALTHY``. Otherwise the
    target data model is the live one that defines a control of the same id.
    Copying a data model preserves control ids verbatim, so this normally
    resolves uniquely and needs no name fuzzing.
    """
    findings: list[Finding] = []
    for param in parameters:
        if param.data_model_id in live_data_model_ids:
            findings.append(Finding(param, HEALTHY, reason="points at a live source"))
            continue

        matches = [
            dm
            for dm in live_data_model_ids
            if param.dm_control_id in controls_by_data_model.get(dm, set())
        ]
        if len(matches) == 1:
            findings.append(
                Finding(
                    param,
                    REPAIRABLE,
                    matches[0],
                    f"live source defines control {param.dm_control_id!r}",
                )
            )
        elif len(matches) > 1:
            findings.append(
                Finding(
                    param,
                    AMBIGUOUS,
                    reason=(
                        f"control {param.dm_control_id!r} is defined by "
                        f"{len(matches)} live sources: {', '.join(matches)}"
                    ),
                )
            )
        elif len(live_data_model_ids) == 1:
            # No control matched, but there is only one place it could point.
            # Flag it: the control was probably renamed or dropped.
            findings.append(
                Finding(
                    param,
                    MISSING_CONTROL,
                    reason=(
                        f"the sole live source {live_data_model_ids[0]} does not "
                        f"define control {param.dm_control_id!r} — it was renamed "
                        f"or removed"
                    ),
                )
            )
        else:
            findings.append(
                Finding(
                    param,
                    MISSING_CONTROL,
                    reason=(
                        f"no live data-model source defines control "
                        f"{param.dm_control_id!r}"
                    ),
                )
            )
    return findings


def apply_repairs(findings: list[Finding]) -> int:
    """Rewrite repairable bindings in place. Returns the number changed."""
    changed = 0
    for finding in findings:
        if finding.status == REPAIRABLE and finding.new_data_model_id:
            finding.parameter.raw["dataModelId"] = finding.new_data_model_id
            changed += 1
    return changed


def spec_to_update_body(spec: dict) -> dict:
    """Reduce a GET spec to the body PUT accepts, dropping read-only metadata."""
    body = {"schemaVersion": spec["schemaVersion"], "pages": spec["pages"]}
    for optional in ("layout", "themeName", "themeOverrides"):
        if optional in spec:
            body[optional] = spec[optional]
    return body


# --------------------------------------------------------------------------
# Sigma API client
# --------------------------------------------------------------------------


class SigmaError(RuntimeError):
    pass


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
            raise SigmaError(
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
                raise SigmaError(
                    f"token exchange failed (HTTP {exc.code}). Check "
                    f"SIGMA_BASE_URL matches your cloud and region, and that the "
                    f"client id/secret are correct."
                ) from exc
            except (KeyError, json.JSONDecodeError) as exc:
                raise SigmaError("token endpoint returned an unexpected body") from exc
        return self._token

    def _call(self, method: str, path: str, body: Any = None) -> Any:
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
            raise SigmaError(f"HTTP {exc.code} on {method} {path}\n{detail}") from exc
        except urllib.error.URLError as exc:
            raise SigmaError(f"could not reach {self.base_url}: {exc.reason}") from exc
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw  # some write endpoints answer in YAML

    def get_workbook_spec(self, workbook_id: str) -> dict:
        return self._call("GET", f"/v2/workbooks/{workbook_id}/spec?format=json")

    def update_workbook_spec(self, workbook_id: str, body: dict) -> Any:
        return self._call("PUT", f"/v2/workbooks/{workbook_id}/spec", body)

    def get_workbook_sources(self, workbook_id: str) -> list[dict]:
        return self._call("GET", f"/v2/workbooks/{workbook_id}/sources")

    def get_data_model_spec(self, data_model_id: str) -> dict:
        return self._call("GET", f"/v2/dataModels/{data_model_id}/spec?format=json")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

_LABEL = {
    HEALTHY: "ok      ",
    REPAIRABLE: "REPAIR  ",
    AMBIGUOUS: "AMBIGUOUS",
    MISSING_CONTROL: "NO MATCH",
}


def analyze(client: SigmaClient, workbook_id: str) -> tuple[dict, list[str], list[Finding]]:
    spec = client.get_workbook_spec(workbook_id)
    sources = client.get_workbook_sources(workbook_id)
    live = [s["dataModelId"] for s in sources if s.get("type") == "data-model"]
    parameters = find_source_parameters(spec)
    controls = {}
    for data_model_id in live:
        controls[data_model_id] = data_model_control_ids(
            client.get_data_model_spec(data_model_id)
        )
    return spec, live, plan_repairs(parameters, live, controls)


def _print_report(spec: dict, live: list[str], findings: list[Finding]) -> None:
    print(f"workbook : {spec.get('name')}  (version {spec.get('documentVersion')})")
    print(f"reads from: {', '.join(live) if live else '(no data-model sources)'}")
    print(f"source parameters: {len(findings)}")
    if not findings:
        print("\nThis workbook has no data-model source parameters.")
        return
    print()
    for finding in findings:
        p = finding.parameter
        print(f"  [{_LABEL[finding.status]}] {p.element_name or p.workbook_control_id!r}"
              f"  (element {p.element_id})")
        print(f"             data model control: {p.dm_control_id}")
        if finding.status == HEALTHY:
            print(f"             {p.data_model_id}")
        elif finding.new_data_model_id:
            print(f"             {p.data_model_id}")
            print(f"          -> {finding.new_data_model_id}")
        else:
            print(f"             {p.data_model_id}  (unresolved)")
        print(f"             {finding.reason}")
        print()


def _summary(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.status] = counts.get(finding.status, 0) + 1
    return counts


def cmd_check(client: SigmaClient, args: argparse.Namespace) -> int:
    spec, live, findings = analyze(client, args.workbook_id)
    if args.json:
        print(json.dumps(
            {
                "workbook": spec.get("name"),
                "workbookId": args.workbook_id,
                "liveDataModelIds": live,
                "summary": _summary(findings),
                "findings": [f.to_dict() for f in findings],
            },
            indent=2,
        ))
    else:
        _print_report(spec, live, findings)
    broken = [f for f in findings if f.needs_attention]
    if not args.json:
        if broken:
            print(f"{len(broken)} source parameter(s) need attention.")
        elif findings:
            print("All source parameters resolve to a live source.")
    return EXIT_FINDINGS if broken else EXIT_OK


def cmd_repair(client: SigmaClient, args: argparse.Namespace) -> int:
    spec, live, findings = analyze(client, args.workbook_id)
    _print_report(spec, live, findings)

    repairable = [f for f in findings if f.status == REPAIRABLE]
    unresolved = [
        f for f in findings if f.status in (AMBIGUOUS, MISSING_CONTROL)
    ]

    if not repairable:
        print("Nothing to repair.")
        return EXIT_FINDINGS if unresolved else EXIT_OK

    if not args.apply:
        print(f"Dry run — {len(repairable)} binding(s) would be rewritten.")
        print("Re-run with --apply to write the change.")
        return EXIT_OK

    changed = apply_repairs(findings)
    client.update_workbook_spec(args.workbook_id, spec_to_update_body(spec))
    print(f"Applied — rewrote {changed} binding(s). A new workbook version was "
          f"created; the previous version is still available in Sigma.")

    _, _, after = analyze(client, args.workbook_id)
    still_broken = [f for f in after if f.needs_attention]
    print(f"Verified — {len(still_broken)} source parameter(s) still need attention.")
    if unresolved:
        print(f"\n{len(unresolved)} binding(s) could not be resolved automatically "
              f"and were left untouched (see above).")
    return EXIT_FINDINGS if still_broken else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sigma-source-params",
        description="Detect and repair broken Sigma workbook source parameters.",
        epilog="Credentials come from SIGMA_BASE_URL, SIGMA_CLIENT_ID and "
               "SIGMA_CLIENT_SECRET.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser(
        "check",
        help="report stale source parameters; exits 1 if any are found",
        description="Report on a workbook's source parameters without changing "
                    "anything. Exits 1 when any binding needs attention, so it "
                    "can gate a rollout pipeline.",
    )
    check.add_argument("workbook_id", help="workbook id or url id")
    check.add_argument("--json", action="store_true", help="emit JSON")
    check.set_defaults(func=cmd_check)

    repair = subparsers.add_parser(
        "repair",
        help="rewrite stale source parameters (dry run unless --apply)",
        description="Rewrite each stale parameters[].dataModelId to the data "
                    "model the workbook actually reads from. Dry run by default.",
    )
    repair.add_argument("workbook_id", help="workbook id or url id")
    repair.add_argument(
        "--apply",
        action="store_true",
        help="write the change (creates a new workbook version)",
    )
    repair.set_defaults(func=cmd_repair)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = SigmaClient.from_env()
        return args.func(client, args)
    except SigmaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG if "environment variable" in str(exc) else EXIT_API
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
