#!/usr/bin/env python3
"""
Run an OpenAI-powered agent to extract admissions and majors URLs for universities
listed in an input Excel file and write results to an output Excel workbook with
three sheets: `Universities`, `Majors`, `Admissions`.

This is the pre-scrape step (steps 1 & 2) requested by the pipeline.
"""
import argparse
import json
import logging
import os
import time
from pathlib import Path

import pandas as pd

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None
    import openai

# Load .env from the pipeline directory if present so `OPENAI_API_KEY` and
# `OPENAI_MODEL` can be provided there. Prefer python-dotenv when available,
# otherwise fall back to a simple parser.
try:
    ENV_PATH = Path(__file__).parent / ".env"
    if ENV_PATH.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(dotenv_path=str(ENV_PATH))
            logging.getLogger(__name__).info("Loaded environment from %s via python-dotenv", ENV_PATH)
        except Exception:
            # simple fallback parser
            with ENV_PATH.open(encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    os.environ.setdefault(k, v)
            logging.getLogger(__name__).info("Loaded environment from %s (manual parse)", ENV_PATH)
except Exception:
    logging.getLogger(__name__).exception("Failed to load .env")

PROMPT_ADMISSIONS = (
    'You are extracting official admissions URLs for {university} in the USA. '
    'Return ONLY a single JSON object, with no Markdown fences, no commentary, no prose.\n\n'
    'Required schema exactly:\n'
    '{{\n'
    '  "university": "string",\n'
    '  "majors": [],\n'
    '  "admissions_urls": [\n'
    '    {{"label": "string", "url": "https://..."}},\n'
    '    {{"label": "string", "url": "https://..."}}\n'
    '  ]\n'
    '}}\n\n'
    'Rules:\n'
    '- Use only official university admissions pages.\n'
    '- Include application process, requirements, deadlines, tuition, financial aid, and contact pages when available.\n'
    '- If you are unsure, omit the URL instead of guessing.\n'
    '- majors must be an empty array [] for this admissions-focused request.\n'
    '- admissions_urls must be a JSON array of objects; never a string, never a list of strings, and never JSON inside a string.\n'
    '- Output must be valid JSON parseable by Python json.loads().\n'
    '- Do not include extra keys or trailing commas.'
)

PROMPT_MAJORS = (
    'You are extracting official undergraduate majors and their direct URLs for {university}. '
    'Return ONLY a single JSON object, with no Markdown fences, no commentary, no prose.\n\n'
    'Required schema exactly:\n'
    '{{\n'
    '  "university": "string",\n'
    '  "majors": [\n'
    '    {{"major_name": "string", "url": "https://..."}},\n'
    '    {{"major_name": "string", "url": "https://..."}}\n'
    '  ],\n'
    '  "admissions_urls": []\n'
    '}}\n\n'
    'Rules:\n'
    '- Use only official university pages.\n'
    '- Each major entry must be an object with major_name and url.\n'
    '- Do not invent majors that do not exist.\n'
    '- Include direct program pages only; avoid general catalogs unless they are the official major listing page.\n'
    '- admissions_urls must be an empty array [] for this majors-focused request.\n'
    '- majors must be a JSON array of objects, never a string and never a nested object wrapped in a list.\n'
    '- Output must be valid JSON parseable by Python json.loads().\n'
    '- Do not include extra keys or trailing commas.'
)


def safe_parse_json(text: str):
    if not text or not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except Exception:
        # attempt to find a JSON object substring
        import re

        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
        return None


def _unwrap_first_mapping(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            unwrapped = _unwrap_first_mapping(item)
            if unwrapped:
                return unwrapped
    return {}


def normalize_payload(payload, fallback_name: str):
    if isinstance(payload, list):
        out = _unwrap_first_mapping(payload)
    elif isinstance(payload, dict):
        out = dict(payload)
    else:
        out = {}

    out.setdefault("university", fallback_name)
    out.setdefault("majors", [])
    out.setdefault("admissions_urls", [])

    for key in ("majors", "admissions_urls"):
        value = out.get(key)
        if isinstance(value, str):
            out[key] = [value]
        elif value is None:
            out[key] = []

    return out


def filter_unprocessed_universities(names, processed_names):
    seen = {str(n).strip() for n in (processed_names or []) if str(n).strip()}
    return [n for n in names if str(n).strip() and str(n).strip() not in seen]


def load_processed_university_names(output_xlsx: Path):
    if not output_xlsx.exists():
        return set()
    try:
        with pd.ExcelFile(output_xlsx) as xls:
            if "Universities" in xls.sheet_names:
                df = pd.read_excel(output_xlsx, sheet_name="Universities", engine="openpyxl")
                if "university_name" in df.columns:
                    return {str(v).strip() for v in df["university_name"].dropna().astype(str) if str(v).strip()}
    except Exception:
        return set()
    return set()


def _openai_error_details(exc) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error") if isinstance(body.get("error"), dict) else body
        msg = error.get("message") if isinstance(error, dict) else str(error)
        code = error.get("code") if isinstance(error, dict) else None
        if msg or code:
            return f"{code}: {msg}" if code else msg

    status = getattr(exc, "status_code", None)
    if status:
        return f"HTTP {status}"
    return str(exc)


def run_agent(input_xlsx: Path, output_xlsx: Path, model: str, rate_delay: float = 1.0):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable not set")

    try:
        client = OpenAI(api_key=api_key) if OpenAI is not None else None
    except Exception:
        client = None

    try:
        uni_df = pd.read_excel(input_xlsx, sheet_name=0, usecols=[0], header=0, engine="openpyxl")
    except Exception:
        raise FileNotFoundError(f"Unable to read input workbook: {input_xlsx}")

    names = uni_df.iloc[:, 0].dropna().astype(str).map(str.strip).replace("", pd.NA).dropna().tolist()

    processed_names = load_processed_university_names(output_xlsx)
    names = filter_unprocessed_universities(names, processed_names)

    universities_rows = []
    majors_rows = []
    admissions_rows = []

    if output_xlsx.exists():
        try:
            with pd.ExcelFile(output_xlsx) as xls:
                for sheet_name in ("Universities", "Majors", "Admissions"):
                    if sheet_name in xls.sheet_names:
                        df = pd.read_excel(output_xlsx, sheet_name=sheet_name, engine="openpyxl")
                        if sheet_name == "Universities":
                            universities_rows.extend(df.to_dict("records"))
                        elif sheet_name == "Majors":
                            majors_rows.extend(df.to_dict("records"))
                        elif sheet_name == "Admissions":
                            admissions_rows.extend(df.to_dict("records"))
        except Exception:
            universities_rows = []
            majors_rows = []
            admissions_rows = []

    for name in names:
        logging.info("Querying LLM for university: %s", name)

        # Admissions prompt
        adm_prompt = PROMPT_ADMISSIONS.format(university=name)
        maj_prompt = PROMPT_MAJORS.format(university=name)

        def ask(prompt: str):
            try:
                if client is not None:
                    resp = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": "Return only valid JSON matching the schema exactly. No explanations, no markdown, no commentary."},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0,
                        max_tokens=1500,
                    )
                    content = resp.choices[0].message.content if resp.choices else None
                    return content

                # Backward compatibility with older OpenAI SDKs.
                openai.api_key = api_key
                resp = openai.ChatCompletion.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "Return only valid JSON matching the schema exactly. No explanations, no markdown, no commentary."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0,
                    max_tokens=1500,
                )
                content = None
                if hasattr(resp, "choices") and resp.choices:
                    first = resp.choices[0]
                    if hasattr(first, "message") and first.message is not None:
                        content = first.message.get("content") if isinstance(first.message, dict) else getattr(first.message, "content", None)
                    else:
                        content = getattr(first, "text", None)
                return content
            except Exception as e:
                details = _openai_error_details(e)
                lowered = (details or str(e)).lower()
                if "credit_balance_exhausted" in lowered or "insufficient_quota" in lowered or "no credits remaining" in lowered:
                    logging.error(
                        "OpenAI billing quota exhausted: the API account has no credits remaining. "
                        "Add credits or switch to a funded API key before running the pipeline. Details: %s",
                        details,
                    )
                    return None
                logging.exception("LLM call failed: %s", e)
                return None

        adm_text = ask(adm_prompt)
        time.sleep(rate_delay)
        maj_text = ask(maj_prompt)
        time.sleep(rate_delay)

        # prefer admissions response first if it contains both keys
        payload = None
        for txt in (adm_text, maj_text):
            if not txt:
                continue
            parsed = safe_parse_json(txt)
            if parsed is not None:
                payload = normalize_payload(parsed, name)
                break

        # fallback: try to merge separate parses
        if payload is None:
            adm_parsed = normalize_payload(safe_parse_json(adm_text or "") or {}, name)
            maj_parsed = normalize_payload(safe_parse_json(maj_text or "") or {}, name)
            payload = {
                "university": adm_parsed.get("university") or maj_parsed.get("university") or name,
                "majors": maj_parsed.get("majors") or adm_parsed.get("majors") or [],
                "admissions_urls": adm_parsed.get("admissions_urls") or maj_parsed.get("admissions_urls") or [],
            }

        # normalize and append
        universities_rows.append({"university_name": name})

        for m in payload.get("majors", []) or []:
            if isinstance(m, str):
                majors_rows.append({"university_name": name, "major_name": str(m).strip(), "major_url": ""})
                continue
            if not isinstance(m, dict):
                continue
            major_name = m.get("major_name") or m.get("name") or m.get("title") or ""
            url = m.get("url") or ""
            majors_rows.append({"university_name": name, "major_name": str(major_name).strip(), "major_url": str(url).strip()})

        for a in payload.get("admissions_urls", []) or []:
            if isinstance(a, str):
                admissions_rows.append({"university_name": name, "label": "", "admissions_url": str(a).strip()})
                continue
            if not isinstance(a, dict):
                continue
            label = a.get("label") or ""
            url = a.get("url") or ""
            admissions_rows.append({"university_name": name, "label": str(label).strip(), "admissions_url": str(url).strip()})

    # write to excel
    with pd.ExcelWriter(output_xlsx, engine="openpyxl") as writer:
        pd.DataFrame(universities_rows, columns=["university_name"]).to_excel(writer, index=False, sheet_name="Universities")
        pd.DataFrame(majors_rows, columns=["university_name", "major_name", "major_url"]).to_excel(writer, index=False, sheet_name="Majors")
        pd.DataFrame(admissions_rows, columns=["university_name", "label", "admissions_url"]).to_excel(writer, index=False, sheet_name="Admissions")

    logging.info("Wrote output workbook: %s", output_xlsx)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="UniversityNames.xlsx", help="Input Excel with university names")
    p.add_argument("--output", default="University_Output1.xlsx", help="Output Excel workbook path")
    p.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4.1"), help="Model name")
    return p.parse_args()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    args = parse_args()
    run_agent(Path(args.input), Path(args.output), args.model)


if __name__ == "__main__":
    main()
