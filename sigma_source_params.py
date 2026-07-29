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

Sigma validates both halves of a binding and matches control ids *exactly*, so a
binding can only be repaired to a control that genuinely exists. Where the tool
cannot determine the right target on its own — a control renamed between template
and clone, or several live models defining the same control id — supply the
missing knowledge with `--map` and `--data-model` rather than letting it guess.

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

__version__ = "1.1.0"

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
            "workbookControlId": p.workbook_control_id,
            "dataModelControlId": p.dm_control_id,
            "currentDataModelId": p.data_model_id,
            "newDataModelId": self.new_data_model_id,
            "newDataModelControlId": self.new_control_id,
            "reason": self.reason,
            "availableControlIds": list(self.available_control_ids),
        }


def parse_control_map(pairs: Iterable[str]) -> dict[str, str]:
    """Parse ``OLD=NEW`` control-id rename pairs.

    Raises ``ValueError`` on a malformed pair so the CLI can complain clearly
    rather than silently ignoring a typo.
    """
    mapping: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(
                f"invalid mapping {pair!r} — expected OLD_CONTROL_ID=NEW_CONTROL_ID"
            )
        old, new = pair.split("=", 1)
        old, new = old.strip(), new.strip()
        if not old or not new:
            raise ValueError(
                f"invalid mapping {pair!r} — both sides must be non-empty"
            )
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


def plan_repairs(
    parameters: list[SourceParameter],
    live_data_model_ids: list[str],
    controls_by_data_model: dict[str, set[str]],
    control_renames: dict[str, str] | None = None,
    preferred_data_model_id: str | None = None,
) -> list[Finding]:
    """Classify each source parameter and choose a repair target.

    A binding is healthy only when its data model is a live source *and* that
    model really defines the referenced control — Sigma validates both halves and
    matches control ids exactly, so a stale control id is just as broken as a
    stale model id.

    ``control_renames`` maps an old data-model control id to its new name, for
    controls deliberately renamed between template and clone.
    ``preferred_data_model_id`` breaks ties when several live models define the
    same control id.
    """
    renames = control_renames or {}
    findings: list[Finding] = []
    all_available = tuple(
        sorted({c for dm in live_data_model_ids for c in controls_by_data_model.get(dm, set())})
    )

    for param in parameters:
        target_control = renames.get(param.dm_control_id, param.dm_control_id)
        was_renamed = target_control != param.dm_control_id
        current_is_live = param.data_model_id in live_data_model_ids
        current_defines_target = target_control in controls_by_data_model.get(
            param.data_model_id, set()
        )

        # Already correct, and no rename asked for.
        if current_is_live and current_defines_target and not was_renamed:
            findings.append(Finding(param, HEALTHY, reason="points at a live source"))
            continue

        # Right model, wrong control name — rewrite the control id in place.
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
            dm
            for dm in live_data_model_ids
            if target_control in controls_by_data_model.get(dm, set())
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
            rename_note = (
                f" (renamed from {param.dm_control_id!r})" if was_renamed else ""
            )
            findings.append(
                Finding(
                    param,
                    REPAIRABLE,
                    candidates[0],
                    target_control if was_renamed else None,
                    f"live source defines control {target_control!r}{rename_note}",
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
                Finding(
                    param,
                    MISSING_CONTROL,
                    reason="the workbook has no live data-model sources",
                )
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
    """Findings that make the workbook unwritable.

    Sigma validates an entire spec on write and rejects any invalid source
    parameter, so a *partial* repair cannot be persisted: one unresolved binding
    fails the whole PUT and nothing changes. Every binding therefore has to be
    resolvable before writing is attempted.
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
    HEALTHY: "ok       ",
    REPAIRABLE: "REPAIR   ",
    AMBIGUOUS: "AMBIGUOUS",
    MISSING_CONTROL: "NO MATCH ",
}


def analyze(
    client: SigmaClient,
    workbook_id: str,
    control_renames: dict[str, str] | None = None,
    preferred_data_model_id: str | None = None,
) -> tuple[dict, list[str], list[Finding]]:
    spec = client.get_workbook_spec(workbook_id)
    sources = client.get_workbook_sources(workbook_id)
    live = [s["dataModelId"] for s in sources if s.get("type") == "data-model"]
    parameters = find_source_parameters(spec)
    controls = {
        data_model_id: data_model_control_ids(client.get_data_model_spec(data_model_id))
        for data_model_id in live
    }
    findings = plan_repairs(
        parameters, live, controls, control_renames, preferred_data_model_id
    )
    return spec, live, findings


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


def _emit_json(workbook_id: str, spec: dict, live: list[str],
               findings: list[Finding], **extra: Any) -> None:
    print(json.dumps(
        {
            "workbook": spec.get("name"),
            "workbookId": workbook_id,
            "liveDataModelIds": live,
            "summary": _summary(findings),
            "findings": [f.to_dict() for f in findings],
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
            raise SigmaError(f"could not read --map-file: {exc}") from exc
        except ValueError as exc:
            raise SigmaError(str(exc)) from exc
    try:
        mapping.update(parse_control_map(getattr(args, "map", None) or []))
    except ValueError as exc:
        raise SigmaError(str(exc)) from exc
    return mapping


def cmd_check(client: SigmaClient, args: argparse.Namespace) -> int:
    renames = _mappings_from_args(args)
    spec, live, findings = analyze(client, args.workbook_id, renames, args.data_model)
    if args.json:
        _emit_json(args.workbook_id, spec, live, findings, appliedMappings=renames)
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
    renames = _mappings_from_args(args)
    spec, live, findings = analyze(client, args.workbook_id, renames, args.data_model)

    repairable = [f for f in findings if f.status == REPAIRABLE]
    unresolved = blocking_findings(findings)

    if not args.json:
        _print_report(spec, live, findings)

    # A partial write is not possible — see blocking_findings().
    if unresolved:
        if args.json:
            _emit_json(args.workbook_id, spec, live, findings, applied=False,
                       blocked=True, wouldRepair=len(repairable),
                       appliedMappings=renames)
        else:
            print(f"Cannot write: {len(unresolved)} binding(s) could not be "
                  f"resolved.")
            print("Sigma validates the whole spec on write, so a partial repair "
                  "cannot be saved — an unresolved binding would reject the "
                  "write and nothing would change.")
            print("Resolve the remaining binding(s) with --map "
                  "OLD_CONTROL_ID=NEW_CONTROL_ID (or --data-model to pick "
                  "between sources), then run again.")
            if repairable:
                print(f"\n{len(repairable)} other binding(s) are ready and will "
                      f"be written in the same pass once the rest resolve.")
        return EXIT_FINDINGS

    if not repairable:
        if args.json:
            _emit_json(args.workbook_id, spec, live, findings,
                       applied=False, appliedMappings=renames)
        else:
            print("Nothing to repair.")
        return EXIT_OK

    if not args.apply:
        if args.json:
            _emit_json(args.workbook_id, spec, live, findings,
                       applied=False, wouldRepair=len(repairable),
                       appliedMappings=renames)
        else:
            print(f"Dry run — {len(repairable)} binding(s) would be rewritten.")
            print("Re-run with --apply to write the change.")
        return EXIT_OK

    changed = apply_repairs(findings)
    client.update_workbook_spec(args.workbook_id, spec_to_update_body(spec))

    after_spec, after_live, after = analyze(
        client, args.workbook_id, renames, args.data_model
    )
    still_broken = [f for f in after if f.needs_attention]

    if args.json:
        _emit_json(args.workbook_id, after_spec, after_live, after,
                   applied=True, repaired=changed,
                   stillNeedingAttention=len(still_broken),
                   appliedMappings=renames)
    else:
        print(f"Applied — rewrote {changed} binding(s). A new workbook version was "
              f"created; the previous version is still available in Sigma.")
        print(f"Verified — {len(still_broken)} source parameter(s) still need "
              f"attention.")
    return EXIT_FINDINGS if still_broken else EXIT_OK


def _add_resolution_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--map",
        action="append",
        metavar="OLD=NEW",
        help="rename a data-model control id, for controls renamed between "
             "template and clone. Repeatable.",
    )
    parser.add_argument(
        "--map-file",
        metavar="PATH",
        help="read control-id renames from a JSON object or OLD=NEW lines",
    )
    parser.add_argument(
        "--data-model",
        metavar="ID",
        help="prefer this data model when several live sources define the same "
             "control id",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")


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
    _add_resolution_flags(check)
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
    _add_resolution_flags(repair)
    repair.set_defaults(func=cmd_repair)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = SigmaClient.from_env()
        return args.func(client, args)
    except SigmaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if "environment variable" in str(exc) or "mapping" in str(exc):
            return EXIT_CONFIG
        return EXIT_API
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
