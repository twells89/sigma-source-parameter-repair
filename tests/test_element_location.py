"""Elements can live in two places, and the tool must read both.

Elements moved from a per-page `pages[].elements` array to a single flat
`document.elements`, with pages reduced to `{id, name}`. A reader that only
knows the old location reports a confident zero on every new document.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sigma_source_params import (  # noqa: E402
    REPAIRABLE,
    apply_repairs,
    data_model_control_targets,
    element_source_map,
    find_source_parameters,
    iter_document_elements,
    plan_repairs,
)

OLD_DM = "11111111-1111-1111-1111-111111111111"
NEW_DM = "22222222-2222-2222-2222-222222222222"
STORE_EL = "storeElement01"


def elements():
    return [
        {"id": "wbStore", "kind": "table",
         "source": {"kind": "data-model", "dataModelId": NEW_DM,
                    "elementId": STORE_EL}},
        {"kind": "control", "id": "ctlCity", "controlId": "Store-City",
         "controlType": "list",
         "source": {"kind": "source",
                    "source": {"kind": "table", "elementId": "wbStore"},
                    "columnId": "cityCol"},
         "parameters": [{"kind": "data-model", "dataModelId": OLD_DM,
                         "controlId": "Store-City"}]},
    ]


def current():
    """Elements at the document root; pages carry only id and name."""
    return {"kind": "workbook", "schemaVersion": 1,
            "layout": '<Page id="p1"><Element elementId="ctlCity"/></Page>',
            "pages": [{"id": "p1", "name": "Page 1"}],
            "elements": elements()}


def legacy():
    """Elements nested per page, as documents were written before the change."""
    return {"kind": "workbook", "schemaVersion": 1,
            "pages": [{"id": "p1", "name": "Page 1", "elements": elements()}]}


class TestIterDocumentElements(unittest.TestCase):
    def test_reads_the_current_top_level_array(self):
        ids = [e.get("id") for e in iter_document_elements(current())]
        self.assertEqual(ids, ["wbStore", "ctlCity"])

    def test_still_reads_the_legacy_per_page_array(self):
        ids = [e.get("id") for e in iter_document_elements(legacy())]
        self.assertEqual(ids, ["wbStore", "ctlCity"])

    def test_reads_both_when_a_document_carries_each(self):
        mixed = dict(current())
        mixed["pages"] = [{"id": "p1", "name": "Page 1",
                           "elements": [{"kind": "control", "id": "extra"}]}]
        ids = sorted(e.get("id") for e in iter_document_elements(mixed))
        self.assertEqual(ids, ["ctlCity", "extra", "wbStore"])

    def test_descends_into_containers_at_the_document_root(self):
        spec = {"pages": [{"id": "p1"}], "elements": [
            {"kind": "container", "id": "box",
             "elements": [{"kind": "control", "id": "inner"}]}]}
        ids = [e.get("id") for e in iter_document_elements(spec)]
        self.assertEqual(ids, ["box", "inner"])

    def test_tolerates_a_document_with_neither(self):
        self.assertEqual(list(iter_document_elements({})), [])
        self.assertEqual(list(iter_document_elements({"pages": [{"id": "p"}]})), [])


class TestBothLocationsBehaveIdentically(unittest.TestCase):
    """Every analysis function must give the same answer for either shape."""

    def test_source_parameters_are_found_in_both(self):
        for name, spec in (("current", current()), ("legacy", legacy())):
            with self.subTest(name):
                found = find_source_parameters(spec)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0].dm_control_id, "Store-City")
                self.assertEqual(found[0].data_model_id, OLD_DM)

    def test_the_value_source_resolves_in_both(self):
        for name, spec in (("current", current()), ("legacy", legacy())):
            with self.subTest(name):
                self.assertEqual(
                    find_source_parameters(spec)[0].source_dm_element_id, STORE_EL)

    def test_element_source_map_agrees(self):
        self.assertEqual(element_source_map(current()),
                         element_source_map(legacy()))

    def test_data_model_control_targets_agree(self):
        self.assertEqual(data_model_control_targets(current()),
                         data_model_control_targets(legacy()))

    def test_a_stale_binding_repairs_in_both(self):
        for name, spec in (("current", current()), ("legacy", legacy())):
            with self.subTest(name):
                findings = plan_repairs(find_source_parameters(spec), [NEW_DM],
                                        {NEW_DM: {"Store-City": STORE_EL}})
                self.assertEqual(findings[0].status, REPAIRABLE)
                self.assertEqual(apply_repairs(findings), 1)

    def test_the_repair_mutates_the_live_document(self):
        spec = current()
        findings = plan_repairs(find_source_parameters(spec), [NEW_DM],
                                {NEW_DM: {"Store-City": STORE_EL}})
        apply_repairs(findings)
        self.assertEqual(spec["elements"][1]["parameters"][0]["dataModelId"], NEW_DM)

    def test_the_layout_is_untouched_by_a_repair(self):
        spec = current()
        before = spec["layout"]
        findings = plan_repairs(find_source_parameters(spec), [NEW_DM],
                                {NEW_DM: {"Store-City": STORE_EL}})
        apply_repairs(findings)
        self.assertEqual(spec["layout"], before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
