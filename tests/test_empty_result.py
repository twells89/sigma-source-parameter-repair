"""A zero result must explain itself.

The worst failure this tool can have is reporting "nothing wrong" because it
misread the response — as a rollout gate that waves a broken document through.
These cover the three ways a zero can arise.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sigma_source_params import (  # noqa: E402
    WORKBOOK,
    __version__,
    count_elements,
    empty_result_note,
)


class TestCountElements(unittest.TestCase):
    def test_counts_legacy_per_page_elements(self):
        spec = {"pages": [
            {"elements": [{"kind": "control", "id": "c1"},
                          {"kind": "table", "id": "t1"}]},
            {"elements": [{"kind": "control", "id": "c2"}]},
        ]}
        self.assertEqual(count_elements(spec), (2, 3, 2))

    def test_counts_top_level_elements(self):
        """Elements moved to a flat document.elements; pages kept only id/name."""
        spec = {"pages": [{"id": "p1", "name": "Page 1"}],
                "elements": [{"kind": "control", "id": "c1"},
                             {"kind": "table", "id": "t1"}]}
        self.assertEqual(count_elements(spec), (1, 2, 1))

    def test_counts_controls_nested_in_containers(self):
        spec = {"pages": [{"id": "p1"}], "elements": [
            {"kind": "container", "id": "box",
             "elements": [{"kind": "control", "id": "c1"}]}
        ]}
        self.assertEqual(count_elements(spec), (1, 2, 1))

    def test_a_misread_response_has_nothing(self):
        self.assertEqual(count_elements({}), (0, 0, 0))
        self.assertEqual(count_elements({"document": {"pages": [{}]}}), (0, 0, 0))

    def test_pages_present_but_elements_missed_is_detectable(self):
        """The exact shape that produced a false 'no controls' report."""
        self.assertEqual(count_elements({"pages": [{"id": "p1", "name": "P"}]}),
                         (1, 0, 0))


class TestEmptyResultNote(unittest.TestCase):
    def test_no_pages_is_reported_as_a_misread_not_a_clean_bill(self):
        note = "\n".join(empty_result_note(WORKBOOK, 0, 0, 0))
        self.assertIn("does not understand the shape", note)
        self.assertIn(__version__, note)

    def test_pages_but_no_elements_is_also_a_misread(self):
        """Elements moved out of pages[] once; missing them must not read clean."""
        note = "\n".join(empty_result_note(WORKBOOK, 1, 0, 0))
        self.assertIn("no elements", note)
        self.assertIn("does not understand the shape", note)

    def test_no_controls_is_stated_plainly(self):
        note = "\n".join(empty_result_note(WORKBOOK, 3, 9, 0))
        self.assertIn("3 page(s)", note)
        self.assertIn("9 element(s)", note)
        self.assertIn("no controls", note)
        self.assertNotIn("does not understand", note)

    def test_controls_but_no_parameters_shows_what_was_scanned(self):
        note = "\n".join(empty_result_note(WORKBOOK, 2, 11, 7))
        self.assertIn("2 page(s)", note)
        self.assertIn("11 element(s)", note)
        self.assertIn("7 control(s)", note)
        self.assertIn(__version__, note)

    def test_every_branch_says_something_checkable(self):
        """No branch may return a bare reassurance with no evidence in it."""
        for args in ((0, 0, 0), (1, 0, 0), (3, 9, 0), (2, 11, 7)):
            note = " ".join(empty_result_note(WORKBOOK, *args))
            self.assertTrue(any(ch.isdigit() for ch in note),
                            f"note for {args} cites no numbers: {note}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestMisreadNeverExitsClean(unittest.TestCase):
    """A response the tool cannot interpret must not look like a passing gate."""

    def test_check_exits_non_zero_when_no_pages_were_found(self):
        import argparse
        from sigma_source_params import (Analysis, Document, EXIT_API,
                                         cmd_check, WORKBOOK)

        class Stub:
            def get_document(self, kind, doc_id):
                return Document(kind, doc_id, {"name": "X", "documentVersion": 1},
                                {}, False)
            def get_sources(self, kind, doc_id):
                return []
            def detect_kind(self, doc_id):
                return WORKBOOK
            def resolve_data_model_id(self, given):
                raise AssertionError("not expected")

        args = argparse.Namespace(document_id="x", type=None, map=None,
                                  map_file=None, data_model=None, json=False)
        self.assertEqual(cmd_check(Stub(), args), EXIT_API)

    def test_check_exits_non_zero_when_pages_exist_but_no_elements(self):
        """The shape that reported a false 'no controls' clean bill."""
        import argparse
        from sigma_source_params import (Document, EXIT_API, cmd_check, WORKBOOK)

        class Stub:
            def get_document(self, kind, doc_id):
                return Document(kind, doc_id, {"name": "X", "documentVersion": 1},
                                {"pages": [{"id": "p1", "name": "Page 1"}]}, True)
            def get_sources(self, kind, doc_id):
                return []
            def detect_kind(self, doc_id):
                return WORKBOOK

        args = argparse.Namespace(document_id="x", type=None, map=None,
                                  map_file=None, data_model=None, json=False)
        self.assertEqual(cmd_check(Stub(), args), EXIT_API)
