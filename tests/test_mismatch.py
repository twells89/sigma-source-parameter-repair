"""Tests for element-mismatch detection.

A source parameter must target a data-model control that filters the same
element the workbook control reads its values from. A control can exist, sit in
a live data model, and still be an invalid target — Sigma rejects the pairing.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sigma_source_params import (  # noqa: E402
    HEALTHY,
    MISMATCH,
    REPAIRABLE,
    blocking_findings,
    data_model_control_targets,
    find_source_parameters,
    plan_repairs,
    workbook_element_sources,
)

OLD_DM = "11111111-1111-1111-1111-111111111111"
NEW_DM = "22222222-2222-2222-2222-222222222222"
OTHER_DM = "33333333-3333-3333-3333-333333333333"

STORE_EL = "storeElement01"
CUSTOMER_EL = "customerElement1"


def workbook(control_reads: str, dm_id: str, dm_control_id: str):
    """A workbook with two tables and one control reading from `control_reads`."""
    return {
        "schemaVersion": 1,
        "pages": [
            {
                "id": "page1",
                "elements": [
                    {
                        "id": "wbStoreTable",
                        "kind": "table",
                        "source": {"kind": "data-model", "dataModelId": NEW_DM,
                                   "elementId": STORE_EL},
                    },
                    {
                        "id": "wbCustomerTable",
                        "kind": "table",
                        "source": {"kind": "data-model", "dataModelId": NEW_DM,
                                   "elementId": CUSTOMER_EL},
                    },
                    {
                        "kind": "control",
                        "id": "aBcDeFgHiJcon",
                        "controlId": "CityControl",
                        "name": "City",
                        "controlType": "list",
                        # The element it *filters* is deliberately different from
                        # the one it reads values from; only the latter counts.
                        "filters": [
                            {"source": {"kind": "table",
                                        "elementId": "wbCustomerTable"},
                             "columnId": "someColumn"}
                        ],
                        "source": {
                            "kind": "source",
                            "source": {"kind": "table", "elementId": control_reads},
                            "columnId": "someColumn",
                        },
                        "parameters": [
                            {"kind": "data-model", "dataModelId": dm_id,
                             "controlId": dm_control_id}
                        ],
                    },
                ],
            }
        ],
    }


# Store-* controls filter the store element, Cust-* the customer element.
TARGETS = {
    "Store-City": STORE_EL,
    "Store-Region": STORE_EL,
    "Cust-City": CUSTOMER_EL,
    "Cust-Region": CUSTOMER_EL,
}


class TestWorkbookElementSources(unittest.TestCase):
    def test_maps_workbook_elements_to_data_model_elements(self):
        spec = workbook("wbStoreTable", NEW_DM, "Store-City")
        self.assertEqual(
            workbook_element_sources(spec),
            {"wbStoreTable": STORE_EL, "wbCustomerTable": CUSTOMER_EL},
        )

    def test_control_records_the_element_it_reads(self):
        spec = workbook("wbCustomerTable", NEW_DM, "Store-City")
        param = find_source_parameters(spec)[0]
        self.assertEqual(param.source_dm_element_id, CUSTOMER_EL)

    def test_filters_do_not_determine_the_value_source(self):
        """The control filters the customer table but reads the store table."""
        spec = workbook("wbStoreTable", NEW_DM, "Store-City")
        param = find_source_parameters(spec)[0]
        self.assertEqual(param.source_dm_element_id, STORE_EL)


class TestDataModelControlTargets(unittest.TestCase):
    def test_reads_target_from_filters(self):
        dm = {"pages": [{"elements": [
            {"kind": "control", "controlId": "Store-City",
             "filters": [{"source": {"elementId": STORE_EL}}]}
        ]}]}
        self.assertEqual(data_model_control_targets(dm), {"Store-City": STORE_EL})

    def test_falls_back_to_value_source(self):
        dm = {"pages": [{"elements": [
            {"kind": "control", "controlId": "Store-City",
             "source": {"kind": "source", "source": {"elementId": STORE_EL}}}
        ]}]}
        self.assertEqual(data_model_control_targets(dm), {"Store-City": STORE_EL})

    def test_unknown_target_is_none(self):
        dm = {"pages": [{"elements": [
            {"kind": "control", "controlId": "Store-City"}
        ]}]}
        self.assertEqual(data_model_control_targets(dm), {"Store-City": None})


class TestMismatchDetection(unittest.TestCase):
    def _plan(self, reads, dm_id, dm_control, renames=None):
        spec = workbook(reads, dm_id, dm_control)
        return plan_repairs(
            find_source_parameters(spec), [NEW_DM], {NEW_DM: TARGETS}, renames
        )

    def test_compatible_binding_is_healthy(self):
        findings = self._plan("wbStoreTable", NEW_DM, "Store-City")
        self.assertEqual(findings[0].status, HEALTHY)

    def test_control_filtering_the_wrong_element_is_a_mismatch(self):
        """The real-world case: control reads customers, targets a store control."""
        findings = self._plan("wbCustomerTable", NEW_DM, "Store-City")
        self.assertEqual(findings[0].status, MISMATCH)

    def test_mismatch_ranks_the_same_named_control_first(self):
        findings = self._plan("wbCustomerTable", NEW_DM, "Store-City")
        self.assertEqual(findings[0].suggested_control_ids[0], "Cust-City")
        self.assertIn("Did you mean 'Cust-City'?", findings[0].reason)
        self.assertIn("--map Store-City=Cust-City", findings[0].reason)

    def test_ranking_is_by_trailing_word_not_position(self):
        findings = self._plan("wbCustomerTable", NEW_DM, "Store-Region")
        self.assertEqual(findings[0].suggested_control_ids[0], "Cust-Region")
        self.assertIn("--map Store-Region=Cust-Region", findings[0].reason)

    def test_all_candidates_are_still_offered(self):
        findings = self._plan("wbCustomerTable", NEW_DM, "Store-City")
        self.assertEqual(
            set(findings[0].suggested_control_ids), {"Cust-City", "Cust-Region"}
        )

    def test_single_suggestion_is_offered_as_a_map_flag(self):
        targets = {"Store-City": STORE_EL, "Cust-City": CUSTOMER_EL}
        spec = workbook("wbCustomerTable", NEW_DM, "Store-City")
        findings = plan_repairs(
            find_source_parameters(spec), [NEW_DM], {NEW_DM: targets}
        )
        self.assertEqual(findings[0].suggested_control_ids, ("Cust-City",))
        self.assertIn("--map Store-City=Cust-City", findings[0].reason)

    def test_stale_model_plus_wrong_element_is_a_mismatch_not_a_repair(self):
        """The common real-world case: stale model id AND an incompatible control.

        Repairing only the model id would produce a spec Sigma still rejects, so
        this must not be reported as repairable.
        """
        findings = self._plan("wbCustomerTable", OLD_DM, "Store-City")
        self.assertEqual(findings[0].status, MISMATCH)
        self.assertIsNone(findings[0].new_data_model_id)

    def test_mapping_to_the_compatible_control_repairs_it(self):
        findings = self._plan(
            "wbCustomerTable", OLD_DM, "Store-City", {"Store-City": "Cust-City"}
        )
        self.assertEqual(findings[0].status, REPAIRABLE)
        self.assertEqual(findings[0].new_data_model_id, NEW_DM)
        self.assertEqual(findings[0].new_control_id, "Cust-City")

    def test_mismatch_blocks_the_write(self):
        findings = self._plan("wbCustomerTable", OLD_DM, "Store-City")
        self.assertEqual(len(blocking_findings(findings)), 1)

    def test_no_suggestion_when_nothing_filters_that_element(self):
        targets = {"Store-City": STORE_EL}
        spec = workbook("wbCustomerTable", NEW_DM, "Store-City")
        findings = plan_repairs(
            find_source_parameters(spec), [NEW_DM], {NEW_DM: targets}
        )
        self.assertEqual(findings[0].status, MISMATCH)
        self.assertEqual(findings[0].suggested_control_ids, ())
        self.assertIn("re-point", findings[0].reason)

    def test_incompatible_model_is_skipped_in_favour_of_a_compatible_one(self):
        spec = workbook("wbCustomerTable", OLD_DM, "Store-City")
        findings = plan_repairs(
            find_source_parameters(spec),
            [NEW_DM, OTHER_DM],
            {NEW_DM: {"Store-City": STORE_EL},
             OTHER_DM: {"Store-City": CUSTOMER_EL}},
        )
        self.assertEqual(findings[0].status, REPAIRABLE)
        self.assertEqual(findings[0].new_data_model_id, OTHER_DM)


class TestUnknownTargetsAreNotFailures(unittest.TestCase):
    """A bare set of control ids carries no target info — do not invent one."""

    def test_set_input_skips_compatibility_checking(self):
        spec = workbook("wbCustomerTable", NEW_DM, "Store-City")
        findings = plan_repairs(
            find_source_parameters(spec), [NEW_DM], {NEW_DM: {"Store-City"}}
        )
        self.assertEqual(findings[0].status, HEALTHY)

    def test_none_target_skips_compatibility_checking(self):
        spec = workbook("wbCustomerTable", NEW_DM, "Store-City")
        findings = plan_repairs(
            find_source_parameters(spec), [NEW_DM], {NEW_DM: {"Store-City": None}}
        )
        self.assertEqual(findings[0].status, HEALTHY)

    def test_control_with_no_value_source_is_not_flagged(self):
        spec = workbook("wbStoreTable", NEW_DM, "Store-City")
        for page in spec["pages"]:
            for element in page["elements"]:
                if element.get("kind") == "control":
                    del element["source"]
        param = find_source_parameters(spec)[0]
        self.assertIsNone(param.source_dm_element_id)
        findings = plan_repairs([param], [NEW_DM], {NEW_DM: TARGETS})
        self.assertEqual(findings[0].status, HEALTHY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
