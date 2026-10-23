#!/usr/bin/env python3
"""
Read the output Excel (Universities/Majors/Admissions), validate reachability,
and write a structured `clgUrls.txt` with only working URLs.

This is the step 3 script in the requested pre-scrape pipeline.
"""
import argparse
import logging
import os
from concurrent.futures import ThreadPoolExecutor
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


def load_cached_valid_urls(path: Path) -> set[str]:
    if not path.exists():
        return set()
    cached = set()
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower() in {"majors:", "admission requirements:"}:
            continue
        if _is_http_url(line):
            cached.add(line.rstrip("/"))
    return cached


def classify_urls_for_validation(current_urls, cached_valid_urls):
    cached_valid_urls = {str(u).strip().rstrip("/") for u in (cached_valid_urls or []) if str(u).strip()}
    current = []
    cached = set()
    for url in current_urls:
        norm = str(url).strip().rstrip("/")
        if not norm:
            continue
        if norm in cached_valid_urls:
            cached.add(norm)
        else:
            current.append(norm)
    return cached, current


def run(input_xlsx: Path, out_txt: Path, timeout: int = 5):
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

    unique_by_uni = {}
    unique_urls = []
    seen_urls = set()
    for uni in unis:
        uni_adm = admissions[admissions["university_name"] == uni]
        uni_maj = majors[majors["university_name"] == uni]

        adm_urls = []
        for url in uni_adm["admissions_url"].tolist():
            u = str(url).strip().rstrip("/")
            if not u:
                continue
            adm_urls.append(u)

        major_urls = []
        for url in uni_maj["major_url"].tolist():
            u = str(url).strip().rstrip("/")
            if not u:
                continue
            major_urls.append(u)

        unique_by_uni[uni] = {
            "admissions": list(dict.fromkeys(adm_urls)),
            "majors": list(dict.fromkeys(major_urls)),
        }

        for u in unique_by_uni[uni]["admissions"] + unique_by_uni[uni]["majors"]:
            if u not in seen_urls:
                seen_urls.add(u)
                unique_urls.append(u)

    previous_valid = load_cached_valid_urls(out_txt)
    cached_urls, urls_to_validate = classify_urls_for_validation(unique_urls, previous_valid)
    url_cache = {url: True for url in cached_urls}

    def validate_url(url: str) -> bool:
        if url in url_cache:
            return url_cache[url]

        session = requests.Session()
        session.headers.update({"User-Agent": "URLScrapePipeline/1.0"})
        result = _is_reachable(session, url, timeout=timeout)
        session.close()
        url_cache[url] = result
        return result

    if urls_to_validate:
        max_workers = min(16, max(1, len(urls_to_validate)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(validate_url, urls_to_validate))
        for url, ok in zip(urls_to_validate, results):
            url_cache[url] = ok

    lines = []
    for uni in unis:
        adm_urls = [u for u in unique_by_uni[uni]["admissions"] if url_cache.get(u, False)]
        major_urls = [u for u in unique_by_uni[uni]["majors"] if url_cache.get(u, False)]

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
    p.add_argument("--timeout", type=int, default=5)
    return p.parse_args()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    args = parse_args()
    run(Path(args.input), Path(args.out), timeout=args.timeout)


if __name__ == "__main__":
    main()
