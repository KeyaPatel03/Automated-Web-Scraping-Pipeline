import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from university_agent_runner import (
    PROMPT_ADMISSIONS,
    PROMPT_MAJORS,
    filter_unprocessed_universities,
    normalize_payload,
)


class NormalizePayloadTests(unittest.TestCase):
    def test_normalizes_nested_list_payloads(self):
        payload = [[{
            "university": "Example University",
            "majors": [{"major_name": "Computer Science", "url": "https://example.edu/cs"}],
            "admissions_urls": [{"label": "Apply", "url": "https://example.edu/apply"}],
        }]]

        normalized = normalize_payload(payload, "Fallback University")

        self.assertEqual(normalized["university"], "Example University")
        self.assertEqual(normalized["majors"][0]["major_name"], "Computer Science")
        self.assertEqual(normalized["admissions_urls"][0]["label"], "Apply")

    def test_prompt_templates_can_format_university_names(self):
        admissions = PROMPT_ADMISSIONS.format(university="Example University")
        majors = PROMPT_MAJORS.format(university="Example University")

        self.assertIn("Example University", admissions)
        self.assertIn("Example University", majors)
        self.assertIn('"university": "string"', admissions)
        self.assertIn('"majors": [', majors)

    def test_filter_unprocessed_universities_skips_existing_names(self):
        processed = {"Example University", "Already Scraped"}
        names = ["Example University", "Already Scraped", "New University"]

        result = filter_unprocessed_universities(names, processed)

        self.assertEqual(result, ["New University"])


if __name__ == "__main__":
    unittest.main()
