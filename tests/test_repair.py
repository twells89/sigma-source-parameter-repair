"""Tests for the spec-analysis core. No network access required."""

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
    data_model_control_ids,
    find_source_parameters,
    iter_elements,
    plan_repairs,
    spec_to_update_body,
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


def data_model(*control_ids):
    return {
        "pages": [
            {
                "id": "page1",
                "elements": [
                    {"kind": "control", "id": f"{c}con", "controlId": c}
                    for c in control_ids
                ],
            }
        ]
    }


class TestIterElements(unittest.TestCase):
    def test_yields_flat_elements(self):
        spec = workbook({"kind": "table", "id": "t1"}, {"kind": "table", "id": "t2"})
        ids = [e["id"] for e in iter_elements(spec["pages"][0]["elements"])]
        self.assertEqual(ids, ["t1", "t2"])

    def test_descends_into_containers(self):
        nested = {
            "kind": "container",
            "id": "outer",
            "elements": [
                {
                    "kind": "container",
                    "id": "inner",
                    "elements": [control("c1", "RegionControl", OLD_DM, "Region")],
                }
            ],
        }
        ids = [e["id"] for e in iter_elements([nested])]
        self.assertEqual(ids, ["outer", "inner", "c1"])

    def test_tolerates_none_and_junk(self):
        self.assertEqual(list(iter_elements(None)), [])
        self.assertEqual(list(iter_elements(["not-a-dict"])), [])


class TestFindSourceParameters(unittest.TestCase):
    def test_finds_parameters_on_controls(self):
        spec = workbook(
            control("c1", "RegionControl", OLD_DM, "Store-Region"),
            {"kind": "table", "id": "t1"},
        )
        found = find_source_parameters(spec)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].element_id, "c1")
        self.assertEqual(found[0].own_control_id, "RegionControl")
        self.assertEqual(found[0].dm_control_id, "Store-Region")
        self.assertEqual(found[0].data_model_id, OLD_DM)

    def test_finds_parameters_nested_in_containers(self):
        spec = workbook(
            {
                "kind": "container",
                "id": "box",
                "elements": [control("c1", "RegionControl", OLD_DM, "Store-Region")],
            }
        )
        self.assertEqual(len(find_source_parameters(spec)), 1)

    def test_ignores_controls_without_parameters(self):
        spec = workbook({"kind": "control", "id": "c1", "controlId": "Plain"})
        self.assertEqual(find_source_parameters(spec), [])

    def test_ignores_non_data_model_parameters(self):
        spec = workbook(
            {
                "kind": "control",
                "id": "c1",
                "controlId": "Plain",
                "parameters": [{"kind": "something-else"}],
            }
        )
        self.assertEqual(find_source_parameters(spec), [])

    def test_prefers_display_name_and_strips_whitespace(self):
        element = control("c1", "RegionControl", OLD_DM, "Store-Region")
        element["name"] = "  Region  "
        found = find_source_parameters(workbook(element))
        self.assertEqual(found[0].element_name, "Region")


class TestDataModelControlIds(unittest.TestCase):
    def test_collects_control_ids(self):
        self.assertEqual(
            data_model_control_ids(data_model("Store-Region", "Store-City")),
            {"Store-Region", "Store-City"},
        )

    def test_empty_model(self):
        self.assertEqual(data_model_control_ids({}), set())


class TestPlanRepairs(unittest.TestCase):
    def _params(self, *elements):
        return find_source_parameters(workbook(*elements))

    def test_healthy_when_already_pointing_at_live_source(self):
        params = self._params(control("c1", "RegionControl", NEW_DM, "Store-Region"))
        findings = plan_repairs(params, [NEW_DM], {NEW_DM: {"Store-Region"}})
        self.assertEqual(findings[0].status, HEALTHY)
        self.assertFalse(findings[0].needs_attention)

    def test_repairable_by_control_id_match(self):
        params = self._params(control("c1", "RegionControl", OLD_DM, "Store-Region"))
        findings = plan_repairs(params, [NEW_DM], {NEW_DM: {"Store-Region"}})
        self.assertEqual(findings[0].status, REPAIRABLE)
        self.assertEqual(findings[0].new_data_model_id, NEW_DM)

    def test_picks_the_matching_model_when_several_are_live(self):
        params = self._params(control("c1", "RegionControl", OLD_DM, "Store-Region"))
        findings = plan_repairs(
            params,
            [OTHER_DM, NEW_DM],
            {OTHER_DM: {"Unrelated"}, NEW_DM: {"Store-Region"}},
        )
        self.assertEqual(findings[0].status, REPAIRABLE)
        self.assertEqual(findings[0].new_data_model_id, NEW_DM)

    def test_ambiguous_when_several_live_models_define_the_control(self):
        params = self._params(control("c1", "RegionControl", OLD_DM, "Store-Region"))
        findings = plan_repairs(
            params,
            [OTHER_DM, NEW_DM],
            {OTHER_DM: {"Store-Region"}, NEW_DM: {"Store-Region"}},
        )
        self.assertEqual(findings[0].status, AMBIGUOUS)
        self.assertIsNone(findings[0].new_data_model_id)

    def test_missing_control_is_not_guessed_at(self):
        """A renamed or dropped control must not be silently rebound."""
        params = self._params(control("c1", "RegionControl", OLD_DM, "Store-Region"))
        findings = plan_repairs(params, [NEW_DM], {NEW_DM: {"Store-District"}})
        self.assertEqual(findings[0].status, MISSING_CONTROL)
        self.assertIsNone(findings[0].new_data_model_id)

    def test_no_live_data_model_sources(self):
        params = self._params(control("c1", "RegionControl", OLD_DM, "Store-Region"))
        findings = plan_repairs(params, [], {})
        self.assertEqual(findings[0].status, MISSING_CONTROL)


class TestApplyRepairs(unittest.TestCase):
    def test_rewrites_only_repairable_bindings_in_place(self):
        spec = workbook(
            control("c1", "RegionControl", OLD_DM, "Store-Region"),
            control("c2", "CityControl", OLD_DM, "Store-City"),
            control("c3", "GoneControl", OLD_DM, "Store-Removed"),
        )
        params = find_source_parameters(spec)
        findings = plan_repairs(
            params, [NEW_DM], {NEW_DM: {"Store-Region", "Store-City"}}
        )
        self.assertEqual(apply_repairs(findings), 2)

        bindings = [
            element["parameters"][0]["dataModelId"]
            for element in spec["pages"][0]["elements"]
        ]
        self.assertEqual(bindings, [NEW_DM, NEW_DM, OLD_DM])

    def test_repair_is_idempotent(self):
        spec = workbook(control("c1", "RegionControl", OLD_DM, "Store-Region"))
        controls = {NEW_DM: {"Store-Region"}}

        findings = plan_repairs(find_source_parameters(spec), [NEW_DM], controls)
        self.assertEqual(apply_repairs(findings), 1)

        again = plan_repairs(find_source_parameters(spec), [NEW_DM], controls)
        self.assertEqual(again[0].status, HEALTHY)
        self.assertEqual(apply_repairs(again), 0)


class TestSpecToUpdateBody(unittest.TestCase):
    def test_drops_read_only_metadata(self):
        spec = dict(
            workbook(control("c1", "RegionControl", NEW_DM, "Store-Region")),
            workbookId="abc",
            name="Example",
            documentVersion=7,
            ownerId="someone",
            createdAt="2026-01-01T00:00:00.000Z",
            layout="<Page/>",
        )
        body = spec_to_update_body(spec)
        self.assertEqual(set(body), {"schemaVersion", "pages", "layout"})

    def test_keeps_theme_fields_when_present(self):
        spec = dict(workbook(), themeName="Dark", themeOverrides={"a": 1})
        body = spec_to_update_body(spec)
        self.assertEqual(body["themeName"], "Dark")
        self.assertEqual(body["themeOverrides"], {"a": 1})

    def test_omits_absent_optional_fields(self):
        body = spec_to_update_body(workbook())
        self.assertEqual(set(body), {"schemaVersion", "pages"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
