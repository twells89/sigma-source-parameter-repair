"""Tests for handling both document kinds — workbooks and data models.

Data models carry source parameters in exactly the same `parameters[]` shape, so
the analysis core is shared. Only the endpoints and two response shapes differ.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sigma_source_params import (  # noqa: E402
    DATA_MODEL,
    REPAIRABLE,
    WORKBOOK,
    SigmaClient,
    SigmaConfigError,
    SigmaError,
    apply_repairs,
    element_source_map,
    find_source_parameters,
    plan_repairs,
    spec_to_update_body,
)

OLD_DM = "11111111-1111-1111-1111-111111111111"
PARENT_DM = "44444444-4444-4444-4444-444444444444"


class FakeClient(SigmaClient):
    """A client whose HTTP layer is a lookup table."""

    def __init__(self, responses):
        super().__init__("https://example.invalid", "id", "secret")
        self.responses = responses
        self.calls = []

    @property
    def token(self):
        return "fake"

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if path not in self.responses:
            raise SigmaError(f"HTTP 404 on {method} {path}")
        value = self.responses[path]
        if isinstance(value, Exception):
            raise value
        return value


def data_model_spec():
    """A data model whose control targets a control in its parent model."""
    return {
        "name": "Team Model",
        "schemaVersion": 1,
        "documentVersion": 2,
        "pages": [{"id": "p1", "elements": [
            {"id": "storeTable", "kind": "table",
             "source": {"kind": "data-model", "dataModelId": PARENT_DM,
                        "elementId": "parentStore"}},
            {"kind": "control", "id": "ctlRegioncon", "controlId": "Store-Region",
             "controlType": "list",
             "source": {"kind": "source",
                        "source": {"kind": "table", "elementId": "storeTable"},
                        "columnId": "regionCol"},
             "parameters": [{"kind": "data-model", "dataModelId": OLD_DM,
                             "controlId": "Region"}]},
        ]}],
    }


class TestSpecToUpdateBody(unittest.TestCase):
    SPEC = {"schemaVersion": 1, "pages": [], "layout": "<Page/>",
            "themeName": "Dark", "themeOverrides": {"a": 1},
            "name": "x", "documentVersion": 3}

    def test_workbook_keeps_layout_and_theme(self):
        body = spec_to_update_body(self.SPEC, WORKBOOK)
        self.assertEqual(set(body),
                         {"schemaVersion", "pages", "layout", "themeName",
                          "themeOverrides"})

    def test_data_model_takes_only_schema_version_and_pages(self):
        """A data model's spec endpoint rejects layout and theme."""
        body = spec_to_update_body(self.SPEC, DATA_MODEL)
        self.assertEqual(set(body), {"schemaVersion", "pages"})

    def test_defaults_to_workbook(self):
        self.assertIn("layout", spec_to_update_body(self.SPEC))


class TestSourcesShape(unittest.TestCase):
    """Workbooks answer with a bare list; data models wrap it in `entries`."""

    def test_workbook_bare_list(self):
        client = FakeClient({
            "/v2/workbooks/wb1/sources": [
                {"type": "data-model", "dataModelId": OLD_DM}
            ]
        })
        self.assertEqual(
            client.get_sources(WORKBOOK, "wb1"),
            [{"type": "data-model", "dataModelId": OLD_DM}],
        )

    def test_data_model_entries_wrapper(self):
        client = FakeClient({
            "/v2/dataModels/dm1/sources": {
                "entries": [{"type": "data-model", "dataModelId": PARENT_DM}],
                "nextPageToken": "",
            }
        })
        self.assertEqual(
            client.get_sources(DATA_MODEL, "dm1"),
            [{"type": "data-model", "dataModelId": PARENT_DM}],
        )

    def test_missing_entries_is_empty_not_an_error(self):
        client = FakeClient({"/v2/dataModels/dm1/sources": {"nextPageToken": ""}})
        self.assertEqual(client.get_sources(DATA_MODEL, "dm1"), [])


class TestDetectKind(unittest.TestCase):
    def test_detects_a_workbook(self):
        client = FakeClient({"/v2/workbooks/abc": {"workbookId": "abc"}})
        self.assertEqual(client.detect_kind("abc"), WORKBOOK)

    def test_detects_a_data_model(self):
        client = FakeClient({"/v2/dataModels/abc": {"dataModelId": "abc"}})
        self.assertEqual(client.detect_kind("abc"), DATA_MODEL)

    def test_unknown_id_is_a_config_error_not_a_raw_400(self):
        """Pointing the tool at a data model used to surface an opaque API error."""
        client = FakeClient({})
        with self.assertRaises(SigmaConfigError) as ctx:
            client.detect_kind("abc")
        self.assertIn("neither a workbook nor a data model", str(ctx.exception))


class TestResolveDataModelId(unittest.TestCase):
    def test_resolves_a_url_id_to_a_uuid(self):
        """--data-model given a url id used to be silently ignored."""
        client = FakeClient({
            "/v2/dataModels/AbCdEf123456GhIjKlMnOp": {"dataModelId": PARENT_DM}
        })
        self.assertEqual(
            client.resolve_data_model_id("AbCdEf123456GhIjKlMnOp"), PARENT_DM
        )

    def test_passes_a_uuid_through(self):
        client = FakeClient({f"/v2/dataModels/{PARENT_DM}": {"dataModelId": PARENT_DM}})
        self.assertEqual(client.resolve_data_model_id(PARENT_DM), PARENT_DM)

    def test_unknown_id_fails_loudly(self):
        client = FakeClient({})
        with self.assertRaises(SigmaConfigError):
            client.resolve_data_model_id("nope")


class TestDataModelSourceParameters(unittest.TestCase):
    """The analysis core works unchanged on a data model spec."""

    def test_finds_parameters_in_a_data_model(self):
        params = find_source_parameters(data_model_spec())
        self.assertEqual(len(params), 1)
        self.assertEqual(params[0].own_control_id, "Store-Region")
        self.assertEqual(params[0].dm_control_id, "Region")
        self.assertEqual(params[0].data_model_id, OLD_DM)

    def test_resolves_the_element_the_control_reads(self):
        params = find_source_parameters(data_model_spec())
        self.assertEqual(params[0].source_dm_element_id, "parentStore")

    def test_element_source_map_works_on_a_data_model(self):
        self.assertEqual(
            element_source_map(data_model_spec()), {"storeTable": "parentStore"}
        )

    def test_a_stale_parameter_is_repaired_against_the_parent_model(self):
        spec = data_model_spec()
        findings = plan_repairs(
            find_source_parameters(spec), [PARENT_DM],
            {PARENT_DM: {"Region": "parentStore"}},
        )
        self.assertEqual(findings[0].status, REPAIRABLE)
        self.assertEqual(findings[0].new_data_model_id, PARENT_DM)
        self.assertEqual(apply_repairs(findings), 1)
        written = spec["pages"][0]["elements"][1]["parameters"][0]
        self.assertEqual(written["dataModelId"], PARENT_DM)


if __name__ == "__main__":
    unittest.main(verbosity=2)
