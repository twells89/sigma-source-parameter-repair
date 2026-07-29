#!/usr/bin/env python3
"""End-to-end test against a live Sigma organization.

Not run in CI — it needs real credentials and it creates and deletes real
documents. Run it by hand before releasing a change that touches resolution
logic or the API client.

It builds its own fixtures, exercises the CLI against them, and removes
everything it made:

  1. clone a data model you point it at, renaming one control
  2. build a workbook bound to the original model's controls
  3. `swapSources` the workbook onto the clone — reproducing the real breakage
  4. assert `check` reports the mixed outcome: repairable + one NO MATCH
  5. assert `repair --apply` refuses to write while anything is unresolved
  6. assert one `--map` unblocks every binding atomically, and is idempotent
  7. assert the result validates server-side
  8. assert a control retargeted at a different element reports MISMATCH,
     suggests the compatible control, and repairs via that suggestion

Usage:
    export SIGMA_BASE_URL=... SIGMA_CLIENT_ID=... SIGMA_CLIENT_SECRET=...
    export SIGMA_E2E_DATA_MODEL_ID=<a data model with at least two controls>
    python3 tests/e2e/test_e2e.py

The source data model is only read, never modified.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)

from sigma_source_params import (  # noqa: E402
    SigmaClient,
    SigmaError,
    data_model_control_ids,
    iter_elements,
)

CLI = os.path.join(REPO, "sigma_source_params.py")
PREFIX = "zz-e2e-source-params"

_failures: list[str] = []
_created: list[tuple[str, str]] = []  # (label, inodeId)


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        _failures.append(label)


def run_cli(*args: str) -> tuple[int, dict]:
    """Run the CLI with --json and return (exit code, parsed payload)."""
    proc = subprocess.run(
        [sys.executable, CLI, *args, "--json"],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if proc.returncode >= 2:
        raise AssertionError(
            f"CLI failed: {' '.join(args)}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    try:
        return proc.returncode, json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"CLI did not emit JSON for {' '.join(args)}: {proc.stdout!r} "
            f"stderr={proc.stderr!r}"
        ) from exc


def statuses(payload: dict) -> dict[str, str]:
    """Map data-model control id -> status."""
    return {f["dataModelControlId"]: f["status"] for f in payload["findings"]}


# --------------------------------------------------------------------------
# Fixture construction
# --------------------------------------------------------------------------


def build_renamed_clone(client: SigmaClient, source_spec: dict, victim: str,
                        renamed: str, folder_id: str) -> str:
    """Clone a data model, renaming one of its controls."""
    spec = json.loads(json.dumps(source_spec))
    for page in spec.get("pages", []):
        for element in iter_elements(page.get("elements")):
            if element.get("kind") == "control" and element.get("controlId") == victim:
                element["controlId"] = renamed
    body = {
        "name": f"{PREFIX}-clone-{int(time.time())}",
        "folderId": folder_id,
        "schemaVersion": spec["schemaVersion"],
        "pages": spec["pages"],
    }
    created = client._call("POST", "/v2/dataModels/spec", body)
    dm_id = created_id(created, "dataModelId") or _find_inode(
        client, body["name"], "dataModels"
    )
    _created.append(("data model", dm_id))
    return dm_id


def dm_control_details(dm_spec: dict) -> dict[str, tuple[str | None, str | None]]:
    """Control id -> (element it filters, column it filters on)."""
    out: dict[str, tuple[str | None, str | None]] = {}
    for page in dm_spec.get("pages", []):
        for element in iter_elements(page.get("elements")):
            if element.get("kind") != "control" or not element.get("controlId"):
                continue
            target = column = None
            for filt in element.get("filters") or []:
                target = (filt.get("source") or {}).get("elementId")
                column = filt.get("columnId")
                if target:
                    break
            out[element["controlId"]] = (target, column)
    return out


def build_workbook(client: SigmaClient, dm_id: str, element_id: str,
                   control_ids: list[str], folder_id: str,
                   source_spec: dict) -> str:
    """A workbook with one table plus one control bound to each DM control.

    The controls carry a real `source`, mirroring how Sigma authors them, and
    each one reads the same column its target data-model control filters on.
    Without that, the workbook does not exercise compatibility checking at all.
    """
    dm_element = next(
        e for page in source_spec["pages"]
        for e in iter_elements(page["elements"])
        if e.get("id") == element_id
    )
    # Workbook columns get their own ids but reuse the data model's formulas.
    formula_to_wb_column, columns = {}, []
    for index, column in enumerate(dm_element.get("columns") or []):
        wb_column_id = f"e2eCol{index:02d}"
        columns.append({"id": wb_column_id, "formula": column["formula"]})
        formula_to_wb_column[column["formula"]] = wb_column_id
    dm_column_formula = {
        c["id"]: c["formula"] for c in (dm_element.get("columns") or [])
    }
    details = dm_control_details(source_spec)

    table_id = "e2eTable01"
    elements: list[dict] = [
        {
            "id": table_id,
            "kind": "table",
            "source": {"kind": "data-model", "dataModelId": dm_id,
                       "elementId": element_id},
            "columns": columns,
            "order": [c["id"] for c in columns],
        }
    ]
    for index, control_id in enumerate(control_ids):
        _, dm_column = details.get(control_id, (None, None))
        wb_column = formula_to_wb_column.get(dm_column_formula.get(dm_column))
        control: dict = {
            "kind": "control",
            "id": f"e2eCtl{index:02d}con",
            "controlId": f"E2EControl{index:02d}",
            "name": control_id,
            "controlType": "list",
            "mode": "include",
            "selectionMode": "multiple",
            "values": [],
            "parameters": [
                {"kind": "data-model", "dataModelId": dm_id,
                 "controlId": control_id}
            ],
        }
        if wb_column:
            control["source"] = {
                "kind": "source",
                "source": {"kind": "table", "elementId": table_id},
                "columnId": wb_column,
            }
        elements.append(control)
    body = {
        "name": f"{PREFIX}-workbook-{int(time.time())}",
        "folderId": folder_id,
        "schemaVersion": 1,
        "pages": [{"id": "e2ePage01", "name": "Page 1", "elements": elements}],
    }
    created = client._call("POST", "/v2/workbooks/spec", body)
    wb_id = created_id(created, "workbookId") or _find_inode(
        client, body["name"], "workbooks"
    )
    _created.append(("workbook", wb_id))
    return wb_id


def build_retargeted_clone(client: SigmaClient, source_spec: dict, victim: str,
                           new_target_element: str, folder_id: str) -> str:
    """Clone a data model, re-pointing one control at a different element.

    Produces the mis-wiring that cannot be authored directly — Sigma rejects a
    workbook whose control reads one element while its parameter targets a
    control filtering another — so it has to be reached via a source swap.
    """
    spec = json.loads(json.dumps(source_spec))
    # A column of the new target element, so the retargeted control stays valid.
    column = next(
        c["id"]
        for page in spec["pages"]
        for e in iter_elements(page["elements"])
        if e.get("id") == new_target_element
        for c in (e.get("columns") or [])
    )
    for page in spec.get("pages", []):
        for element in iter_elements(page.get("elements")):
            if element.get("kind") != "control" or element.get("controlId") != victim:
                continue
            element["filters"] = [
                {"source": {"kind": "table", "elementId": new_target_element},
                 "columnId": column}
            ]
            element["source"] = {
                "kind": "source",
                "source": {"kind": "table", "elementId": new_target_element},
                "columnId": column,
            }
    body = {
        "name": f"{PREFIX}-retargeted-{int(time.time())}",
        "folderId": folder_id,
        "schemaVersion": spec["schemaVersion"],
        "pages": spec["pages"],
    }
    created = client._call("POST", "/v2/dataModels/spec", body)
    dm_id = created_id(created, "dataModelId") or _find_inode(
        client, body["name"], "dataModels"
    )
    _created.append(("data model", dm_id))
    return dm_id


def created_id(response: Any, key: str) -> str | None:
    """Pull an id out of a create response.

    The spec write endpoints answer in YAML (``success: true`` plus the new id),
    so a JSON-shaped read is not enough.
    """
    if isinstance(response, dict):
        return response.get(key)
    if isinstance(response, str):
        for line in response.splitlines():
            name, _, value = line.partition(":")
            if name.strip() == key and value.strip():
                return value.strip()
    return None


def controls_by_target_element(dm_spec: dict) -> dict[str, list[str]]:
    """Group a data model's control ids by the element each one targets.

    A workbook source parameter can only bind to a data-model control whose
    target element the workbook itself includes, so a fixture has to pair them
    up rather than picking controls at random.
    """
    grouped: dict[str, list[str]] = {}
    for page in dm_spec.get("pages", []):
        for element in iter_elements(page.get("elements")):
            if element.get("kind") != "control" or not element.get("controlId"):
                continue
            target = None
            for filt in element.get("filters") or []:
                target = (filt.get("source") or {}).get("elementId")
                if target:
                    break
            if target is None:
                source = element.get("source") or {}
                target = (source.get("source") or {}).get("elementId")
            if target:
                grouped.setdefault(target, []).append(element["controlId"])
    return grouped


def _find_inode(client: SigmaClient, name: str, collection: str) -> str:
    listing = client._call("GET", f"/v2/{collection}?limit=500")
    key = "workbookId" if collection == "workbooks" else "dataModelId"
    for entry in listing.get("entries", []):
        if entry.get("name") == name:
            return entry[key]
    raise AssertionError(f"could not locate the {collection} entry named {name!r}")


def swap_workbook_source(client: SigmaClient, wb_id: str, from_dm: str,
                         to_dm: str, element_ids: list[str]) -> None:
    mapping = [
        {
            "from": {"type": "data-model", "dataModelId": from_dm,
                     "elementId": element_id},
            "to": {"type": "data-model", "dataModelId": to_dm,
                   "elementId": element_id},
        }
        for element_id in element_ids
    ]
    client._call(
        "POST", f"/v3alpha/workbooks/{wb_id}:swapSources",
        {"sourceMapping": mapping},
    )


def cleanup(client: SigmaClient) -> None:
    """Remove everything the run created.

    Also sweeps by name, so an artifact whose id we never learned — because the
    run died between creating it and parsing the response — still gets removed
    rather than leaking into the org.
    """
    print("\ncleanup")
    removed = set()
    for label, inode_id in reversed(_created):
        if not inode_id:
            continue
        try:
            client._call("DELETE", f"/v2/files/{inode_id}")
            removed.add(inode_id)
            print(f"  removed {label} {inode_id}")
        except SigmaError as exc:
            print(f"  WARNING could not remove {label} {inode_id}: {exc}")

    for collection, key in (("workbooks", "workbookId"), ("dataModels", "dataModelId")):
        try:
            listing = client._call("GET", f"/v2/{collection}?limit=500")
        except SigmaError:
            continue
        for entry in listing.get("entries", []):
            inode_id = entry.get(key)
            if not entry.get("name", "").startswith(PREFIX) or inode_id in removed:
                continue
            try:
                client._call("DELETE", f"/v2/files/{inode_id}")
                print(f"  swept stray {collection[:-1]} {entry['name']}")
            except SigmaError as exc:
                print(f"  WARNING could not sweep {entry['name']}: {exc}")


# --------------------------------------------------------------------------


def main() -> int:
    source_dm = os.environ.get("SIGMA_E2E_DATA_MODEL_ID")
    if not source_dm:
        print("error: set SIGMA_E2E_DATA_MODEL_ID to a data model with at least "
              "two controls", file=sys.stderr)
        return 2

    client = SigmaClient.from_env()
    source_spec = client.get_data_model_spec(source_dm)
    folder_id = os.environ.get("SIGMA_E2E_FOLDER_ID") or source_spec["folderId"]

    all_controls = data_model_control_ids(source_spec)
    if len(all_controls) < 2:
        print(f"error: source data model defines {len(all_controls)} control(s); "
              f"need at least 2", file=sys.stderr)
        return 2

    # Pair the workbook's table element with controls that actually target it.
    grouped = controls_by_target_element(source_spec)
    if not grouped:
        print("error: could not determine which element each control targets",
              file=sys.stderr)
        return 2
    table_element, controls = max(grouped.items(), key=lambda kv: len(kv[1]))
    controls = sorted(controls)
    if len(controls) < 2:
        print(f"error: no single data model element has 2+ controls targeting it; "
              f"best was {table_element} with {len(controls)}", file=sys.stderr)
        return 2

    victim = controls[0]
    renamed = f"{victim}-Renamed"
    keepers = controls[1:3]  # a couple of untouched controls for contrast
    used = [victim, *keepers]

    print(f"source data model : {source_dm}")
    print(f"controls in play  : {', '.join(used)}")
    print(f"renaming          : {victim} -> {renamed}\n")

    try:
        print("setup")
        clone_dm = build_renamed_clone(
            client, source_spec, victim, renamed, folder_id
        )
        print(f"  created clone data model {clone_dm}")
        wb_id = build_workbook(client, source_dm, table_element, used, folder_id,
                               source_spec)
        print(f"  created workbook {wb_id}")

        print("\n1. a freshly built workbook is healthy")
        code, payload = run_cli("check", wb_id)
        check("check exits 0", code == 0, f"exit={code}")
        check(
            "every binding healthy",
            set(statuses(payload).values()) == {"healthy"},
            json.dumps(statuses(payload)),
        )

        print("\n2. swapSources reproduces the breakage")
        swap_workbook_source(client, wb_id, source_dm, clone_dm, [table_element])
        code, payload = run_cli("check", wb_id)
        found = statuses(payload)
        check("check exits 1", code == 1, f"exit={code}")
        check(
            "the workbook now reads from the clone",
            payload["liveDataModelIds"] == [clone_dm],
            json.dumps(payload["liveDataModelIds"]),
        )
        check(
            "untouched controls are repairable",
            all(found.get(c) == "repairable" for c in keepers),
            json.dumps(found),
        )
        check(
            "the renamed control is NO MATCH, not a guess",
            found.get(victim) == "missing-control",
            json.dumps(found),
        )
        no_match = next(
            f for f in payload["findings"] if f["dataModelControlId"] == victim
        )
        check(
            "NO MATCH lists the available control ids",
            renamed in no_match["availableControlIds"],
            json.dumps(no_match["availableControlIds"]),
        )

        print("\n3. repair refuses to write while anything is unresolved")
        # Sigma validates the whole spec, so a partial write is impossible. The
        # tool must detect that up front rather than attempting a doomed PUT.
        code, payload = run_cli("repair", wb_id, "--apply")
        check("repair exits 1", code == 1, f"exit={code}")
        check("it reports being blocked", payload.get("blocked") is True,
              json.dumps({k: payload.get(k) for k in ("blocked", "applied")}))
        check("nothing was written", payload.get("applied") is False,
              f"applied={payload.get('applied')}")
        check(
            "it still counts the bindings that are ready",
            payload.get("wouldRepair") == len(keepers),
            f"wouldRepair={payload.get('wouldRepair')}",
        )
        _, unchanged = run_cli("check", wb_id)
        check(
            "the workbook is untouched on the server",
            statuses(unchanged) == found,
            f"before={json.dumps(found)} after={json.dumps(statuses(unchanged))}",
        )

        print("\n4. one mapping unblocks the whole repair, atomically")
        code, payload = run_cli(
            "repair", wb_id, "--map", f"{victim}={renamed}", "--apply"
        )
        check("repair exits 0", code == 0, f"exit={code}")
        check(
            f"repaired all {len(used)} bindings in one pass",
            payload["repaired"] == len(used),
            f"repaired={payload['repaired']}",
        )
        check(
            "nothing needs attention any more",
            payload["stillNeedingAttention"] == 0,
            f"remaining={payload['stillNeedingAttention']}",
        )

        print("\n5. the repair is idempotent")
        code, payload = run_cli("check", wb_id)
        check("check exits 0", code == 0, f"exit={code}")
        check(
            "all bindings healthy",
            set(statuses(payload).values()) == {"healthy"},
            json.dumps(statuses(payload)),
        )
        code, payload = run_cli("repair", wb_id, "--apply")
        check("a second repair changes nothing", code == 0 and not payload.get("applied"),
              f"exit={code} applied={payload.get('applied')}")

        print("\n6. the result validates server-side")
        # POST rejects any spec containing an invalid source parameter, so a
        # successful round-trip is Sigma's own confirmation that the bindings
        # are sound. Nothing is created when it rejects.
        spec = client.get_workbook_spec(wb_id)
        probe = {
            "name": f"{PREFIX}-validation-probe-{int(time.time())}",
            "folderId": folder_id,
            "schemaVersion": spec["schemaVersion"],
            "pages": spec["pages"],
        }
        try:
            created = client._call("POST", "/v2/workbooks/spec", probe)
            _created.append(("workbook", created_id(created, "workbookId")))
            check("Sigma accepts the repaired spec", True)
        except SigmaError as exc:
            check("Sigma accepts the repaired spec", False, str(exc))

        print("\n7. a control filtering the wrong element is a MISMATCH")
        others = [el for el in grouped if el != table_element and grouped[el]]
        if not others:
            print("  SKIP  the source data model has controls on only one "
                  "element, so this case cannot be built here")
        else:
            other_element = others[0]
            victim2 = keepers[0]
            print(f"  re-pointing {victim2} at element {other_element}")
            retargeted = build_retargeted_clone(
                client, source_spec, victim2, other_element, folder_id
            )
            wb2 = build_workbook(
                client, source_dm, table_element, [victim2], folder_id, source_spec
            )
            # Valid until the swap moves the control's target out from under it.
            code, payload = run_cli("check", wb2)
            check("the mis-wiring is not detectable before the swap", code == 0,
                  json.dumps(statuses(payload)))

            swap_workbook_source(client, wb2, source_dm, retargeted, [table_element])
            code, payload = run_cli("check", wb2)
            found2 = statuses(payload)
            check("check exits 1", code == 1, f"exit={code}")
            check("reported as element-mismatch, not repairable",
                  found2.get(victim2) == "element-mismatch", json.dumps(found2))

            finding = payload["findings"][0]
            check("it names the element the control actually reads",
                  finding["readsDataModelElement"] is not None,
                  json.dumps(finding))
            check("it suggests a control filtering the right element",
                  bool(finding["suggestedControlIds"]),
                  json.dumps(finding["suggestedControlIds"]))
            check("every suggestion really filters that element",
                  all(s in grouped[table_element]
                      for s in finding["suggestedControlIds"]),
                  f"suggested={finding['suggestedControlIds']} "
                  f"valid={grouped[table_element]}")

            code, payload = run_cli("repair", wb2, "--apply")
            check("repair refuses to write a mismatch", code == 1, f"exit={code}")
            check("nothing was written", payload.get("applied") is False,
                  f"applied={payload.get('applied')}")

            suggestion = finding["suggestedControlIds"][0]
            code, payload = run_cli(
                "repair", wb2, "--map", f"{victim2}={suggestion}", "--apply"
            )
            check("the suggested mapping repairs it", code == 0, f"exit={code}")
            check("nothing needs attention any more",
                  payload["stillNeedingAttention"] == 0,
                  f"remaining={payload['stillNeedingAttention']}")

    finally:
        cleanup(client)

    print()
    if _failures:
        print(f"FAILED — {len(_failures)} assertion(s): {'; '.join(_failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
