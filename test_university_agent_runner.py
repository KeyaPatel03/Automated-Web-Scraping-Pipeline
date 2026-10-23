import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from filter_and_write_clgurls import classify_urls_for_validation
from scrape_pipeline import existing_urls_from_file
from university_agent_runner import (
    PROMPT_ADMISSIONS,
    PROMPT_MAJORS,
    build_chat_completion_kwargs,
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

    def test_classify_urls_for_validation_reuses_cached_urls(self):
        current_urls = [
            "https://example.edu/admissions",
            "https://example.edu/majors",
            "https://new.example.edu/apply",
        ]
        existing_urls = {
            "https://example.edu/admissions",
            "https://example.edu/old",
        }

        cached, to_validate = classify_urls_for_validation(current_urls, existing_urls)

        self.assertEqual(cached, {"https://example.edu/admissions"})
        self.assertEqual(to_validate, ["https://example.edu/majors", "https://new.example.edu/apply"])

    def test_merge_of_admissions_and_majors_is_preserved(self):
        adm_text = '{"university": "Example University", "majors": [], "admissions_urls": [{"label": "Apply", "url": "https://example.edu/apply"}]}'
        maj_text = '{"university": "Example University", "majors": [{"major_name": "Computer Science", "url": "https://example.edu/cs"}], "admissions_urls": []}'

        adm_parsed = normalize_payload(__import__('json').loads(adm_text), "Example University")
        maj_parsed = normalize_payload(__import__('json').loads(maj_text), "Example University")

        merged = {
            "university": adm_parsed.get("university") or maj_parsed.get("university") or "Example University",
            "majors": maj_parsed.get("majors") or adm_parsed.get("majors") or [],
            "admissions_urls": adm_parsed.get("admissions_urls") or maj_parsed.get("admissions_urls") or [],
        }

        self.assertEqual(len(merged["majors"]), 1)
        self.assertEqual(merged["majors"][0]["major_name"], "Computer Science")
        self.assertEqual(len(merged["admissions_urls"]), 1)
        self.assertEqual(merged["admissions_urls"][0]["label"], "Apply")

    def test_reasoning_models_use_max_completion_tokens(self):
        kwargs = build_chat_completion_kwargs(model="gpt-5-mini", temperature=0, max_tokens=1500)

        self.assertNotIn("max_tokens", kwargs)
        self.assertEqual(kwargs["max_completion_tokens"], 1500)
        self.assertNotIn("temperature", kwargs)

    def test_scraper_ignores_section_headers_and_invalid_entries(self):
        content = """# Example University - Majors/Admission requirements
Majors:
https://example.edu/majors

Admission requirements:
https://example.edu/admissions
not-a-url
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            urls_file = Path(temp_dir) / "clgUrls.txt"
            urls_file.write_text(content, encoding="utf-8")

            self.assertEqual(
                existing_urls_from_file(urls_file),
                ["https://example.edu/majors", "https://example.edu/admissions"],
            )


if __name__ == "__main__":
    unittest.main()
