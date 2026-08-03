"""Tests for translating Sigma's rejection into guidance.

The tool does not predict whether Sigma will accept a target — validity depends
on filter reachability through the model's join graph, which the code
representation does not expose. So Sigma judges and these functions interpret.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sigma_source_params import (  # noqa: E402
    candidates_for,
    explain_rejection,
    find_source_parameters,
    parse_invalid_parameter,
)

DM = "22222222-2222-2222-2222-222222222222"
STORE_EL = "storeElement01"
CUSTOMER_EL = "customerElement1"

MESSAGE = (
    "Invalid parameter on control: aBcDeFgHiJcon targeting data model: "
    f"{DM}, controlId: Store-City."
)

TARGETS = {
    "Store-City": STORE_EL,
    "Store-Region": STORE_EL,
    "Cust-City": CUSTOMER_EL,
    "Cust-Region": CUSTOMER_EL,
}


def spec(reads_element):
    return {
        "schemaVersion": 1,
        "pages": [{"id": "p1", "elements": [
            {"id": "wbStore", "kind": "table",
             "source": {"kind": "data-model", "dataModelId": DM,
                        "elementId": STORE_EL}},
            {"id": "wbCust", "kind": "table",
             "source": {"kind": "data-model", "dataModelId": DM,
                        "elementId": CUSTOMER_EL}},
            {"kind": "control", "id": "aBcDeFgHiJcon", "controlId": "CityControl",
             "name": "City", "controlType": "list",
             "source": {"kind": "source",
                        "source": {"kind": "table", "elementId": reads_element},
                        "columnId": "someCol"},
             "parameters": [{"kind": "data-model", "dataModelId": DM,
                             "controlId": "Store-City"}]},
        ]}],
    }


class TestParseInvalidParameter(unittest.TestCase):
    def test_extracts_all_three_ids(self):
        self.assertEqual(
            parse_invalid_parameter(MESSAGE),
            {"elementId": "aBcDeFgHiJcon", "dataModelId": DM,
             "controlId": "Store-City"},
        )

    def test_tolerates_a_wrapping_http_prefix(self):
        wrapped = f"HTTP 400 on PUT /v2/workbooks/abc/spec\n{MESSAGE}"
        self.assertEqual(
            parse_invalid_parameter(wrapped)["controlId"], "Store-City"
        )

    def test_handles_a_control_id_containing_spaces(self):
        msg = ("Invalid parameter on control: xYzcon targeting data model: "
               f"{DM}, controlId: Store City Name.")
        self.assertEqual(parse_invalid_parameter(msg)["controlId"], "Store City Name")

    def test_returns_none_for_an_unrelated_message(self):
        self.assertIsNone(parse_invalid_parameter("HTTP 500 something broke"))
        self.assertIsNone(parse_invalid_parameter(""))


class TestCandidatesFor(unittest.TestCase):
    def test_offers_controls_filtering_the_element_the_control_reads(self):
        param = find_source_parameters(spec("wbCust"))[0]
        self.assertEqual(
            candidates_for(param, TARGETS, "Store-City"),
            ("Cust-City", "Cust-Region"),
        )

    def test_likest_name_comes_first(self):
        param = find_source_parameters(spec("wbCust"))[0]
        self.assertEqual(
            candidates_for(param, TARGETS, "Store-Region")[0], "Cust-Region"
        )

    def test_no_candidates_when_the_element_is_unknown(self):
        param = find_source_parameters(spec("wbStore"))[0]
        param.source_dm_element_id = None
        self.assertEqual(candidates_for(param, TARGETS, "Store-City"), ())

    def test_no_candidates_when_nothing_filters_that_element(self):
        param = find_source_parameters(spec("wbCust"))[0]
        self.assertEqual(
            candidates_for(param, {"Store-City": STORE_EL}, "Store-City"), ()
        )


class TestExplainRejection(unittest.TestCase):
    def _explain(self, reads_element, targets=TARGETS, message=MESSAGE):
        params = find_source_parameters(spec(reads_element))
        return explain_rejection(message, params, {DM: targets})

    def test_names_the_element_each_side_uses(self):
        text = self._explain("wbCust")
        self.assertIn("aBcDeFgHiJcon", text)
        self.assertIn(CUSTOMER_EL, text)
        self.assertIn(STORE_EL, text)

    def test_suggests_a_mapping_to_a_compatible_control(self):
        text = self._explain("wbCust")
        self.assertIn("--map Store-City=Cust-City", text)

    def test_does_not_claim_the_user_is_wrong(self):
        """It reports that the write API declined, and offers the UI."""
        text = self._explain("wbCust")
        self.assertIn("write API", text)
        self.assertIn("Sigma UI", text)

    def test_reports_a_genuinely_absent_control_differently(self):
        text = self._explain("wbCust", targets={"Cust-City": CUSTOMER_EL})
        self.assertIn("does not define", text)
        self.assertIn("Cust-City", text)
        self.assertNotIn("--map", text)

    def test_passes_an_unrecognised_message_through_untouched(self):
        self.assertEqual(
            self._explain("wbCust", message="HTTP 503 upstream unavailable"),
            "HTTP 503 upstream unavailable",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
