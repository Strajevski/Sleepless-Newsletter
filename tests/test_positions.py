import copy
import json
import unittest
from pathlib import Path

from circus_newsletter.editor import EditorFailure, issue_from_draft, validate_draft
from circus_newsletter.captioner import _issue_context
from circus_newsletter.render import _group_positions


class PositionValidationTests(unittest.TestCase):
    def test_model_schema_requires_all_declared_properties(self):
        schema = json.loads((Path(__file__).resolve().parents[1] /
                             "config/editor-output.schema.json").read_text(encoding="utf-8"))
        def check(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertEqual(set(node["properties"]), set(node["required"]))
                    self.assertIs(node["additionalProperties"], False)
                for child in node.values():
                    check(child)
            elif isinstance(node, list):
                for child in node:
                    check(child)
        check(schema)

    def setUp(self):
        self.records = [
            {"submission_id": f"{name.lower()}-001", "member_id": name.lower(),
             "display_name": name, "body": "An explicit position.", "submitted_links": []}
            for name in ("Alex", "Bob", "Charlie")
        ]
        self.draft = {"issue_week": "2026-W40", "title": "Title", "summary": "Summary",
                      "sleepless_take": "Editorial", "gallery": [], "feed": [],
                      "personal_updates": [], "worth_your_time": [],
                      "long_and_short": {
                          "long": [{"text": "Lorem ipsum", "source_ids": ["alex-001", "bob-001"]}],
                          "short": [{"text": "Lorem ipsum", "source_ids": ["charlie-001"]}]}}

    def test_sources_supply_names_and_count_towards_coverage(self):
        validate_draft(self.draft, "2026-W40", self.records)
        issue = issue_from_draft(self.draft, self.records)
        self.assertEqual(issue["long_and_short"]["long"][0]["contributors"], ["Alex", "Bob"])
        self.assertEqual(issue["long_and_short"]["short"][0]["contributors"], ["Charlie"])
        self.assertEqual(_issue_context(issue, "bob-001"), [{"section": "long", "text": "Lorem ipsum"}])
        self.assertEqual(_group_positions(issue["long_and_short"]["long"])[0]["attribution"], "Alex, Bob")

    def test_invalid_positions_are_rejected(self):
        for invalid in (None, [], {}, {"long": [], "short": "bad"},
                        {"long": [{"text": "", "source_ids": ["alex-001"]}], "short": []},
                        {"long": [{"text": "Text", "source_ids": []}], "short": []},
                        {"long": [{"text": "Text", "source_ids": ["unknown-001"]}], "short": []},
                        {"long": [{"text": "Text", "source_ids": [{}]}], "short": []},
                        {"long": [{"text": "Text", "source_ids": ["alex-001"],
                                   "contributors": ["Invented"]}], "short": []}):
            with self.subTest(invalid=invalid):
                draft = copy.deepcopy(self.draft)
                draft["long_and_short"] = invalid
                with self.assertRaises(EditorFailure):
                    validate_draft(draft, "2026-W40", self.records)

    def test_missing_and_empty_sources_are_rejected(self):
        self.draft["long_and_short"]["short"] = []
        with self.assertRaisesRegex(EditorFailure, "omitted"):
            validate_draft(self.draft, "2026-W40", self.records)
        self.records[0]["body"] = ""
        with self.assertRaisesRegex(EditorFailure, "empty"):
            validate_draft(self.draft, "2026-W40", self.records)

    def test_old_drafts_still_validate(self):
        self.draft.pop("long_and_short")
        self.draft["personal_updates"] = [
            {"member_id": record["member_id"], "source_ids": [record["submission_id"]], "text": "Update"}
            for record in self.records]
        validate_draft(self.draft, "2026-W40", self.records)
        self.assertEqual(issue_from_draft(self.draft, self.records)["long_and_short"], {"long": [], "short": []})
