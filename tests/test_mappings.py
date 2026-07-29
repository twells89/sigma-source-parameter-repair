"""Tests for explicit rename mappings and data-model preference.

These cover the escape hatches that supply knowledge the tool cannot derive:
a control renamed between template and clone, and a tie between live sources.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sigma_source_params import (  # noqa: E402
    AMBIGUOUS,
    HEALTHY,
    MISSING_CONTROL,
    REPAIRABLE,
    apply_repairs,
    blocking_findings,
    find_source_parameters,
    load_control_map_file,
    parse_control_map,
    plan_repairs,
)

OLD_DM = "11111111-1111-1111-1111-111111111111"
NEW_DM = "22222222-2222-2222-2222-222222222222"
OTHER_DM = "33333333-3333-3333-3333-333333333333"


def control(element_id, workbook_control_id, dm_id, dm_control_id):
    return {
        "kind": "control",
        "id": element_id,
        "controlId": workbook_control_id,
        "controlType": "list",
        "parameters": [
            {"kind": "data-model", "dataModelId": dm_id, "controlId": dm_control_id}
        ],
    }


def workbook(*elements):
    return {
        "schemaVersion": 1,
        "pages": [{"id": "page1", "name": "Page 1", "elements": list(elements)}],
    }


class TestParseControlMap(unittest.TestCase):
    def test_parses_pairs(self):
        self.assertEqual(
            parse_control_map(["Store-City=Store-Municipality", "A=B"]),
            {"Store-City": "Store-Municipality", "A": "B"},
        )

    def test_tolerates_surrounding_whitespace(self):
        self.assertEqual(parse_control_map([" A = B "]), {"A": "B"})

    def test_keeps_later_value_for_repeated_key(self):
        self.assertEqual(parse_control_map(["A=B", "A=C"]), {"A": "C"})

    def test_allows_equals_in_the_new_name(self):
        self.assertEqual(parse_control_map(["A=B=C"]), {"A": "B=C"})

    def test_rejects_pair_without_separator(self):
        with self.assertRaises(ValueError):
            parse_control_map(["NoSeparator"])

    def test_rejects_empty_side(self):
        with self.assertRaises(ValueError):
            parse_control_map(["=B"])
        with self.assertRaises(ValueError):
            parse_control_map(["A="])


class TestLoadControlMapFile(unittest.TestCase):
    def test_reads_json_object(self):
        self.assertEqual(
            load_control_map_file('{"Store-City": "Store-Municipality"}'),
            {"Store-City": "Store-Municipality"},
        )

    def test_reads_pair_lines_ignoring_comments_and_blanks(self):
        text = "# renames\nStore-City=Store-Municipality\n\nA=B\n"
        self.assertEqual(
            load_control_map_file(text),
            {"Store-City": "Store-Municipality", "A": "B"},
        )

    def test_rejects_malformed_json(self):
        with self.assertRaises(ValueError):
            load_control_map_file("{not json")

    def test_rejects_non_string_json_values(self):
        with self.assertRaises(ValueError):
            load_control_map_file('{"A": 1}')


class TestRenameMapping(unittest.TestCase):
    def _params(self, *elements):
        return find_source_parameters(workbook(*elements))

    def test_rename_makes_a_no_match_repairable(self):
        params = self._params(control("c1", "RegionControl", OLD_DM, "Store-Region"))
        controls = {NEW_DM: {"Store-District"}}

        without = plan_repairs(params, [NEW_DM], controls)
        self.assertEqual(without[0].status, MISSING_CONTROL)

        with_map = plan_repairs(
            params, [NEW_DM], controls, {"Store-Region": "Store-District"}
        )
        self.assertEqual(with_map[0].status, REPAIRABLE)
        self.assertEqual(with_map[0].new_data_model_id, NEW_DM)
        self.assertEqual(with_map[0].new_control_id, "Store-District")

    def test_rename_within_the_same_live_model(self):
        """The model id is already right; only the control was renamed."""
        params = self._params(control("c1", "RegionControl", NEW_DM, "Store-Region"))
        controls = {NEW_DM: {"Store-District"}}
        findings = plan_repairs(
            params, [NEW_DM], controls, {"Store-Region": "Store-District"}
        )
        self.assertEqual(findings[0].status, REPAIRABLE)
        self.assertEqual(findings[0].new_data_model_id, NEW_DM)
        self.assertEqual(findings[0].new_control_id, "Store-District")

    def test_a_rename_to_a_nonexistent_control_is_still_no_match(self):
        """Mapping is not a licence to invent — the target must exist."""
        params = self._params(control("c1", "RegionControl", OLD_DM, "Store-Region"))
        findings = plan_repairs(
            params, [NEW_DM], {NEW_DM: {"Store-District"}},
            {"Store-Region": "Store-Nowhere"},
        )
        self.assertEqual(findings[0].status, MISSING_CONTROL)

    def test_irrelevant_mappings_are_ignored(self):
        params = self._params(control("c1", "RegionControl", OLD_DM, "Store-Region"))
        findings = plan_repairs(
            params, [NEW_DM], {NEW_DM: {"Store-Region"}}, {"Unrelated": "Whatever"}
        )
        self.assertEqual(findings[0].status, REPAIRABLE)
        self.assertIsNone(findings[0].new_control_id)

    def test_apply_writes_both_ids(self):
        spec = workbook(control("c1", "RegionControl", OLD_DM, "Store-Region"))
        findings = plan_repairs(
            find_source_parameters(spec),
            [NEW_DM],
            {NEW_DM: {"Store-District"}},
            {"Store-Region": "Store-District"},
        )
        self.assertEqual(apply_repairs(findings), 1)
        written = spec["pages"][0]["elements"][0]["parameters"][0]
        self.assertEqual(written["dataModelId"], NEW_DM)
        self.assertEqual(written["controlId"], "Store-District")

    def test_renamed_repair_is_idempotent(self):
        spec = workbook(control("c1", "RegionControl", OLD_DM, "Store-Region"))
        controls = {NEW_DM: {"Store-District"}}
        renames = {"Store-Region": "Store-District"}

        first = plan_repairs(find_source_parameters(spec), [NEW_DM], controls, renames)
        self.assertEqual(apply_repairs(first), 1)

        # The stale name is gone, so the mapping no longer applies and the
        # binding reads as healthy.
        second = plan_repairs(find_source_parameters(spec), [NEW_DM], controls, renames)
        self.assertEqual(second[0].status, HEALTHY)
        self.assertEqual(apply_repairs(second), 0)


class TestStaleControlIdIsDetected(unittest.TestCase):
    """A live model id is not enough — Sigma validates the control id too."""

    def test_live_model_but_missing_control_is_not_healthy(self):
        params = find_source_parameters(
            workbook(control("c1", "RegionControl", NEW_DM, "Store-Gone"))
        )
        findings = plan_repairs(params, [NEW_DM], {NEW_DM: {"Store-Region"}})
        self.assertEqual(findings[0].status, MISSING_CONTROL)

    def test_no_match_reports_the_available_control_ids(self):
        params = find_source_parameters(
            workbook(control("c1", "RegionControl", OLD_DM, "Store-Gone"))
        )
        findings = plan_repairs(
            params, [NEW_DM], {NEW_DM: {"Store-Region", "Store-City"}}
        )
        self.assertEqual(
            findings[0].available_control_ids, ("Store-City", "Store-Region")
        )

    def test_control_can_be_rebound_to_a_different_live_model(self):
        """Current model is live but lacks the control; another live one has it."""
        params = find_source_parameters(
            workbook(control("c1", "RegionControl", NEW_DM, "Store-Region"))
        )
        findings = plan_repairs(
            params,
            [NEW_DM, OTHER_DM],
            {NEW_DM: {"Unrelated"}, OTHER_DM: {"Store-Region"}},
        )
        self.assertEqual(findings[0].status, REPAIRABLE)
        self.assertEqual(findings[0].new_data_model_id, OTHER_DM)


class TestBlockingFindings(unittest.TestCase):
    """Sigma validates the whole spec, so a partial repair cannot be written."""

    def _findings(self, controls, renames=None):
        params = find_source_parameters(
            workbook(
                control("c1", "RegionControl", OLD_DM, "Store-Region"),
                control("c2", "CityControl", OLD_DM, "Store-City"),
            )
        )
        return plan_repairs(params, [NEW_DM], controls, renames)

    def test_all_repairable_is_writable(self):
        findings = self._findings({NEW_DM: {"Store-Region", "Store-City"}})
        self.assertEqual(blocking_findings(findings), [])

    def test_one_unresolvable_blocks_the_write(self):
        findings = self._findings({NEW_DM: {"Store-Region"}})
        blocked = blocking_findings(findings)
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].parameter.dm_control_id, "Store-City")

    def test_mapping_the_straggler_unblocks_the_write(self):
        findings = self._findings(
            {NEW_DM: {"Store-Region", "Store-Municipality"}},
            {"Store-City": "Store-Municipality"},
        )
        self.assertEqual(blocking_findings(findings), [])
        self.assertEqual(apply_repairs(findings), 2)

    def test_ambiguity_also_blocks(self):
        params = find_source_parameters(
            workbook(control("c1", "RegionControl", OLD_DM, "Store-Region"))
        )
        findings = plan_repairs(
            params,
            [NEW_DM, OTHER_DM],
            {NEW_DM: {"Store-Region"}, OTHER_DM: {"Store-Region"}},
        )
        self.assertEqual(len(blocking_findings(findings)), 1)

    def test_healthy_bindings_never_block(self):
        params = find_source_parameters(
            workbook(control("c1", "RegionControl", NEW_DM, "Store-Region"))
        )
        findings = plan_repairs(params, [NEW_DM], {NEW_DM: {"Store-Region"}})
        self.assertEqual(blocking_findings(findings), [])


class TestPreferredDataModel(unittest.TestCase):
    def _ambiguous_params(self):
        return find_source_parameters(
            workbook(control("c1", "RegionControl", OLD_DM, "Store-Region"))
        )

    def test_data_model_breaks_a_tie(self):
        controls = {OTHER_DM: {"Store-Region"}, NEW_DM: {"Store-Region"}}

        without = plan_repairs(self._ambiguous_params(), [OTHER_DM, NEW_DM], controls)
        self.assertEqual(without[0].status, AMBIGUOUS)

        with_pref = plan_repairs(
            self._ambiguous_params(),
            [OTHER_DM, NEW_DM],
            controls,
            preferred_data_model_id=NEW_DM,
        )
        self.assertEqual(with_pref[0].status, REPAIRABLE)
        self.assertEqual(with_pref[0].new_data_model_id, NEW_DM)

    def test_ambiguous_finding_names_the_candidates(self):
        findings = plan_repairs(
            self._ambiguous_params(),
            [OTHER_DM, NEW_DM],
            {OTHER_DM: {"Store-Region"}, NEW_DM: {"Store-Region"}},
        )
        self.assertEqual(
            set(findings[0].available_control_ids), {OTHER_DM, NEW_DM}
        )

    def test_preference_for_a_model_lacking_the_control_is_not_honoured(self):
        """--data-model selects among valid candidates; it cannot force a bad one."""
        findings = plan_repairs(
            self._ambiguous_params(),
            [OTHER_DM, NEW_DM],
            {OTHER_DM: {"Store-Region"}, NEW_DM: {"Something-Else"}},
            preferred_data_model_id=NEW_DM,
        )
        self.assertEqual(findings[0].status, REPAIRABLE)
        self.assertEqual(findings[0].new_data_model_id, OTHER_DM)

    def test_preference_combines_with_a_rename(self):
        findings = plan_repairs(
            self._ambiguous_params(),
            [OTHER_DM, NEW_DM],
            {OTHER_DM: {"Store-District"}, NEW_DM: {"Store-District"}},
            {"Store-Region": "Store-District"},
            preferred_data_model_id=NEW_DM,
        )
        self.assertEqual(findings[0].status, REPAIRABLE)
        self.assertEqual(findings[0].new_data_model_id, NEW_DM)
        self.assertEqual(findings[0].new_control_id, "Store-District")


if __name__ == "__main__":
    unittest.main(verbosity=2)
