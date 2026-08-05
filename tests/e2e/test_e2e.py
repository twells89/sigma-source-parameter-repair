#!/usr/bin/env python3
"""End-to-end test against a live Sigma organization.

Not run in CI — it needs real credentials and it creates and deletes real
documents. Run it by hand before releasing a change that touches resolution
logic or the API client.

It builds its own fixtures, exercises the CLI against them, and removes
everything it made:

  1-2. clone a data model (renaming one control), build a workbook against the
       original, and `swapSources` onto the clone — the real breakage
  3.   assert `check` reports repairable bindings plus one NO MATCH
  4.   assert `repair --apply` refuses to write while anything is unresolved
  5.   assert one `--map` unblocks every binding atomically, is idempotent, and
       leaves the layout byte-identical (a write that omits `layout` is accepted
       and Sigma regenerates it, moving every element)
  6.   assert the result validates server-side
  7.   assert a rejection from Sigma is translated into guidance rather than
       predicted up front
  8.   assert data models are handled too — they carry the same `parameters[]`

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
    DATA_MODEL,
    WORKBOOK,
    SigmaClient,
    SigmaError,
    data_model_control_ids,
    iter_elements,
    unwrap_document,
)

WORKBOOK_KIND, DATA_MODEL_KIND = WORKBOOK, DATA_MODEL

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


def run_cli_text(*args: str) -> tuple[int, str]:
    """Run the CLI without --json and return (exit code, stdout+stderr)."""
    proc = subprocess.run(
        [sys.executable, CLI, *args],
        capture_output=True, text=True, env=os.environ.copy(),
    )
    return proc.returncode, proc.stdout + proc.stderr


def statuses(payload: dict) -> dict[str, str]:
    """Map data-model control id -> status."""
    return {f["dataModelControlId"]: f["status"] for f in payload["findings"]}


# --------------------------------------------------------------------------
# Fixture construction
# --------------------------------------------------------------------------


def post_spec(client: SigmaClient, kind: str, name: str, folder_id: str,
              document: dict) -> str:
    """Create a workbook or data model, tolerating either body shape.

    Workbooks moved to a `document` wrapper; data models have not. Rather than
    hard-code which is which, try wrapped and fall back to flat on a shape error.
    """
    base = "workbooks" if kind == WORKBOOK else "dataModels"
    key = "workbookId" if kind == WORKBOOK else "dataModelId"
    attempts = [
        {"name": name, "folderId": folder_id, "document": document},
        {"name": name, "folderId": folder_id, **document},
    ]
    last = None
    for body in attempts:
        try:
            created = client.call("POST", f"/v2/{base}/spec", body)
        except SigmaError as exc:
            # Either shape can be the wrong one, and the endpoints disagree on
            # how they say so ("Expecting { schemaVersion: 1 } at 0.document"
            # vs "Syntax error in data model spec"). Retry rather than parse.
            last = exc
            if "HTTP 400" in str(exc):
                continue
            raise
        doc_id = created_id(created, key) or _find_inode(client, name, base)
        _created.append((kind, doc_id))
        return doc_id
    raise AssertionError(f"could not create {kind} {name!r}: {last}")


def get_content(client: SigmaClient, kind: str, doc_id: str) -> dict:
    """The writable document, whichever shape the endpoint returns."""
    content, _ = unwrap_document(client.get_spec(kind, doc_id))
    return content


def build_renamed_clone(client: SigmaClient, source_spec: dict, victim: str,
                        renamed: str, folder_id: str) -> str:
    """Clone a data model, renaming one of its controls."""
    spec = json.loads(json.dumps(source_spec))
    for page in spec.get("pages", []):
        for element in iter_elements(page.get("elements")):
            if element.get("kind") == "control" and element.get("controlId") == victim:
                element["controlId"] = renamed
    return post_spec(
        client, DATA_MODEL, f"{PREFIX}-clone-{int(time.time())}", folder_id,
        {k: v for k, v in spec.items() if k in ("kind", "schemaVersion", "pages")},
    )


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
    # An explicit layout, so the fixture actually exercises layout preservation.
    # Earlier fixtures had none, which is why a layout-destroying write went
    # unnoticed by this test.
    rows = [f'    <LayoutElement elementId="{e["id"]}" '
            f'gridColumn="1 / 13" gridRow="{i * 3 + 1} / {i * 3 + 4}"/>'
            for i, e in enumerate(elements)]
    layout = ('<?xml version="1.0" encoding="utf-8"?>\n'
              '<Page type="grid" gridTemplateColumns="repeat(24, 1fr)" '
              'gridTemplateRows="auto" id="e2ePage01">\n'
              + "\n".join(rows) + "\n</Page>\n")

    return post_spec(
        client, WORKBOOK, f"{PREFIX}-workbook-{int(time.time())}", folder_id,
        {
            "kind": "workbook",
            "schemaVersion": 1,
            "layout": layout,
            "pages": [{"id": "e2ePage01", "name": "Page 1", "elements": elements}],
        },
    )


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
    return post_spec(
        client, DATA_MODEL, f"{PREFIX}-retargeted-{int(time.time())}", folder_id,
        {k: v for k, v in spec.items() if k in ("kind", "schemaVersion", "pages")},
    )


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


def swap_source(client: SigmaClient, kind: str, document_id: str, from_dm: str,
                to_dm: str, element_ids: list[str]) -> None:
    """Re-point a workbook OR a data model at a different data model."""
    base = "workbooks" if kind == WORKBOOK else "dataModels"
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
        "POST", f"/v3alpha/{base}/{document_id}:swapSources",
        {"sourceMapping": mapping},
    )


def build_child_data_model(client: SigmaClient, parent_dm: str, parent_element: str,
                           parent_control: str, folder_id: str,
                           parent_spec: dict) -> str:
    """A data model whose control drives a control in its PARENT data model.

    This is the data-model equivalent of the workbook case: the same
    `parameters[]` binding, one level up the stack.
    """
    dm_element = next(
        e for page in parent_spec["pages"]
        for e in iter_elements(page["elements"])
        if e.get("id") == parent_element
    )
    formula_to_column, columns = {}, []
    for index, column in enumerate(dm_element.get("columns") or []):
        column_id = f"e2eDmCol{index:02d}"
        columns.append({"id": column_id, "formula": column["formula"]})
        formula_to_column[column["formula"]] = column_id
    parent_formula = {
        c["id"]: c["formula"] for c in (dm_element.get("columns") or [])
    }
    _, parent_column = dm_control_details(parent_spec).get(
        parent_control, (None, None)
    )
    own_column = formula_to_column.get(parent_formula.get(parent_column))

    table_id = "e2eDmTable01"
    control: dict = {
        "kind": "control",
        "id": "e2eDmCtl00con",
        "controlId": "E2EChildControl",
        "controlType": "list",
        "mode": "include",
        "selectionMode": "multiple",
        "values": [],
        "parameters": [
            {"kind": "data-model", "dataModelId": parent_dm,
             "controlId": parent_control}
        ],
    }
    if own_column:
        control["source"] = {
            "kind": "source",
            "source": {"kind": "table", "elementId": table_id},
            "columnId": own_column,
        }
        control["filters"] = [
            {"source": {"kind": "table", "elementId": table_id},
             "columnId": own_column}
        ]
    body = {
        "name": f"{PREFIX}-child-dm-{int(time.time())}",
        "folderId": folder_id,
        "schemaVersion": 1,
        "pages": [{"id": "e2eDmPage01", "name": "Page 1", "elements": [
            {
                "id": table_id,
                "kind": "table",
                "source": {"kind": "data-model", "dataModelId": parent_dm,
                           "elementId": parent_element},
                "columns": columns,
                "order": [c["id"] for c in columns],
            },
            control,
        ]}],
    }
    return post_spec(client, DATA_MODEL, body.pop("name"), body.pop("folderId"), body)


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
    source_spec = client.get_spec(DATA_MODEL, source_dm)
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
        swap_source(client, WORKBOOK_KIND, wb_id, source_dm, clone_dm,
                    [table_element])
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
        layout_before = get_content(client, WORKBOOK, wb_id).get("layout")
        check("the fixture has a layout to preserve",
              bool((layout_before or "").strip()),
              "without one, layout preservation is not being tested at all")
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

        layout_after = get_content(client, WORKBOOK, wb_id).get("layout")
        check("the layout is byte-identical after the repair",
              layout_after == layout_before,
              f"before {len(layout_before or '')} chars, "
              f"after {len(layout_after or '')} chars — elements moved")
        check("the tool itself reports the layout unchanged",
              payload.get("layoutUnchanged") is True,
              f"layoutUnchanged={payload.get('layoutUnchanged')}")

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
        content = get_content(client, WORKBOOK, wb_id)
        try:
            post_spec(client, WORKBOOK,
                      f"{PREFIX}-validation-probe-{int(time.time())}",
                      folder_id, content)
            check("Sigma accepts the repaired spec", True)
        except SigmaError as exc:
            check("Sigma accepts the repaired spec", False, str(exc))

        print("\n7. a rejection from Sigma is translated, not predicted")
        # The tool no longer guesses whether Sigma will accept a target: validity
        # depends on filter reachability through the model's join graph, which the
        # code representation does not expose. So drive a real rejection and check
        # the tool explains it. The mis-wiring cannot be authored directly (Sigma
        # refuses it), so it is reached via a retargeted clone plus a source swap.
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
            code, payload = run_cli("check", wb2)
            check("healthy before the swap", code == 0,
                  json.dumps(statuses(payload)))

            swap_source(client, WORKBOOK_KIND, wb2, source_dm, retargeted,
                        [table_element])

            # Identity-wise this resolves fine, so the tool will attempt the write.
            code, payload = run_cli("check", wb2)
            check("the tool considers it resolvable",
                  statuses(payload).get(victim2) == "repairable",
                  json.dumps(statuses(payload)))

            code, text = run_cli_text("repair", wb2, "--apply")
            check("repair exits non-zero when Sigma refuses", code == 1,
                  f"exit={code}")
            check("it says the write was refused and nothing changed",
                  "refused" in text and "nothing changed" in text, text[:300])
            check("it names the offending element",
                  "e2eCtl00con" in text, text[:300])
            check("it explains this is the write API declining, not a bad id",
                  "write API" in text, text[:300])
            check("it offers the controls that filter the element it reads",
                  "--map" in text, text[:300])
            check("it points at the UI as the alternative",
                  "Sigma UI" in text, text[:300])

            after_code, after = run_cli("check", wb2)
            check("the document is untouched on the server",
                  after["findings"][0]["currentDataModelId"] == source_dm,
                  json.dumps(after["findings"][0]))

        print("\n8. data models carry source parameters too")
        # A data model's controls take the identical parameters[] shape, so the
        # same breakage and the same repair apply one level up the stack.
        mid_clone = build_renamed_clone(
            client, source_spec, "\x00none\x00", "\x00none\x00", folder_id
        )
        child = build_child_data_model(
            client, source_dm, table_element, used[0], folder_id, source_spec
        )
        code, payload = run_cli("check", child)
        check("a data model id is accepted, not an opaque API error", code == 0,
              json.dumps(payload)[:300])
        check("detected as a data model",
              payload.get("documentKind") == "data model",
              str(payload.get("documentKind")))
        check("its source parameter is healthy to begin with",
              set(statuses(payload).values()) == {"healthy"},
              json.dumps(statuses(payload)))

        swap_source(client, DATA_MODEL_KIND, child, source_dm, mid_clone,
                    [table_element])
        code, payload = run_cli("check", child)
        check("check exits 1 after the swap", code == 1, f"exit={code}")
        check("the stale binding is repairable",
              set(statuses(payload).values()) == {"repairable"},
              json.dumps(statuses(payload)))

        code, payload = run_cli("repair", child, "--apply")
        check("repair exits 0", code == 0, f"exit={code}")
        check("it repaired the binding", payload["repaired"] == 1,
              f"repaired={payload['repaired']}")
        check("nothing needs attention any more",
              payload["stillNeedingAttention"] == 0,
              f"remaining={payload['stillNeedingAttention']}")

        code, payload = run_cli("repair", child, "--apply")
        check("a second data model repair changes nothing",
              code == 0 and not payload.get("applied"),
              f"exit={code} applied={payload.get('applied')}")

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
