#!/usr/bin/env python3
"""
Read the output Excel (Universities/Majors/Admissions), validate reachability,
and write a structured `clgUrls.txt` with only working URLs.

This is the step 3 script in the requested pre-scrape pipeline.
"""
import argparse
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests


def _is_http_url(value: str) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _looks_like_error_page(text: str) -> bool:
    if not isinstance(text, str) or not text:
        return True
    lowered = text.lower()
    patterns = [
        "page not found",
        "404 not found",
        "requested url was not found",
        "the page you were looking for",
        "we can't find",
        "this page could not be found",
        "error page",
        "<title>404",
        "<title>error",
        "not found",
    ]
    return any(p in lowered for p in patterns)


def _is_reachable(session: requests.Session, url: str, timeout: int = 10) -> bool:
    if not _is_http_url(url):
        return False
    try:
        resp = session.get(url, allow_redirects=True, timeout=timeout)
        if resp.status_code >= 400:
            return False
        return not _looks_like_error_page(resp.text)
    except requests.RequestException:
        return False


def run(input_xlsx: Path, out_txt: Path, timeout: int = 10):
    try:
        admissions = pd.read_excel(input_xlsx, sheet_name="Admissions", engine="openpyxl")
    except Exception:
        admissions = pd.DataFrame(columns=["university_name", "label", "admissions_url"])

    try:
        majors = pd.read_excel(input_xlsx, sheet_name="Majors", engine="openpyxl")
    except Exception:
        majors = pd.DataFrame(columns=["university_name", "major_name", "major_url"])

    # Normalize columns
    for df, cols in ((admissions, ["university_name", "label", "admissions_url"]), (majors, ["university_name", "major_name", "major_url"])):
        for c in cols:
            if c not in df.columns:
                df[c] = ""
        df[cols] = df[cols].fillna("")

    # list of universities
    unis = pd.concat([admissions["university_name"], majors["university_name"]]).astype(str).str.strip()
    unis = [u for u in unis.unique() if u]

    session = requests.Session()
    session.headers.update({"User-Agent": "URLScrapePipeline/1.0"})

    lines = []
    for uni in unis:
        uni_adm = admissions[admissions["university_name"] == uni]
        uni_maj = majors[majors["university_name"] == uni]

        adm_urls = []
        for url in uni_adm["admissions_url"].tolist():
            u = str(url).strip().rstrip("/")
            if not u:
                continue
            if _is_reachable(session, u, timeout=timeout):
                adm_urls.append(u)

        major_urls = []
        for url in uni_maj["major_url"].tolist():
            u = str(url).strip().rstrip("/")
            if not u:
                continue
            if _is_reachable(session, u, timeout=timeout):
                major_urls.append(u)

        if not adm_urls and not major_urls:
            continue

        # Header
        lines.append(f"# {uni} - Majors/Admission requirements")

        if major_urls:
            lines.append("Majors:")
            for u in major_urls:
                lines.append(u)

        if adm_urls:
            lines.append("")
            lines.append("Admission requirements:")
            for u in adm_urls:
                lines.append(u)

        lines.append("")

    out_txt.parent.mkdir(parents=True, exist_ok=True)
    out_txt.write_text("\n".join(lines), encoding="utf-8")
    logging.info("Wrote clgUrls file: %s (entries: %d)", out_txt, len(lines))


def parse_args():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--input", default="University_Output1.xlsx")
    p.add_argument("--out", default="clgUrls.txt")
    p.add_argument("--timeout", type=int, default=10)
    return p.parse_args()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    args = parse_args()
    run(Path(args.input), Path(args.out), timeout=args.timeout)


if __name__ == "__main__":
    main()
