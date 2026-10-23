#!/usr/bin/env python3
"""
Incremental URL scraping pipeline.

Reads URLs from `clgUrls.txt`, scrapes each URL (HTML or PDF), saves per-URL .txt files
into `scraped_texts/`, and maintains an incremental combined text and PDF in
`output/combined/`.

Behavior:
- Skip re-scraping when possible by using HTTP `ETag`/`Last-Modified` headers or by
  comparing content hashes.
- Remove outputs for URLs removed from `clgUrls.txt`.
- Update the combined file incrementally by replacing only changed URL sections.

Dependencies: `requests`, `beautifulsoup4`, `pdfminer.six`, `reportlab` (optional for PDF)
"""
import hashlib
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
import logging
import subprocess

try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except Exception:
    pdf_extract_text = None

try:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas
except Exception:
    canvas = None

BASE_DIR = Path(__file__).parent
URLS_FILE = BASE_DIR / "clgUrls.txt"
SCRAPED_DIR = BASE_DIR / "scraped_texts"
OUTPUT_DIR = BASE_DIR / "output"
COMBINED_DIR = OUTPUT_DIR / "combined"
COMBINED_TXT = COMBINED_DIR / "combined.txt"
COMBINED_PDF = COMBINED_DIR / "combined.pdf"

SEG_START = "<!-- START URL: {url} ID: {id} -->"
SEG_END = "<!-- END URL: {url} ID: {id} -->"


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def ensure_dirs():
    SCRAPED_DIR.mkdir(parents=True, exist_ok=True)
    COMBINED_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging(level=logging.INFO):
    handler = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(handler)
    root.setLevel(level)


logger = logging.getLogger(__name__)


def read_urls():
    return existing_urls_from_file(URLS_FILE)


def persist_urls(urls):
    seen = set()
    ordered = []
    for url in urls:
        u = str(url).strip()
        if not u or u in seen:
            continue
        seen.add(u)
        ordered.append(u)
    URLS_FILE.write_text("\n".join(ordered) + ("\n" if ordered else ""), encoding="utf-8")
    logger.info("Updated %s with %d URLs", URLS_FILE.name, len(ordered))


def get_head(session, url):
    try:
        return session.head(url, allow_redirects=True, timeout=15)
    except Exception:
        return None


def get_get(session, url):
    try:
        return session.get(url, allow_redirects=True, timeout=30)
    except Exception:
        return None


def is_pdf_response(resp):
    if resp is None:
        return False
    ct = resp.headers.get("content-type", "").lower()
    return "application/pdf" in ct


def extract_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for s in soup(['script', 'style', 'noscript']):
        s.extract()

    # Prefer semantic main/article containers when available
    main_el = soup.find('main') or soup.find(attrs={"role": "main"}) or soup.find('article')
    selected = None
    if main_el:
        selected = main_el
        logger.debug("extract_text_from_html: using <main>/<role=main>/article element")
    else:
        # Fallback: choose the largest block element (by text length) that is
        # not inside nav/header/footer/aside. This helps avoid repeated site
        # navigation text like "Skip To Main Content" appearing prominently.
        candidates = []
        for tag in soup.find_all(['div', 'section', 'article', 'main', 'body'], recursive=True):
            # skip small or invisible
            txt = tag.get_text(separator="\n").strip()
            if len(txt) < 200:
                continue
            # skip if inside nav/header/footer/aside
            if tag.find_parent(['nav', 'header', 'footer', 'aside']):
                continue
            candidates.append((len(txt), tag))
        if candidates:
            candidates.sort(reverse=True, key=lambda x: x[0])
            selected = candidates[0][1]
            logger.debug("extract_text_from_html: using largest content block (len=%d)", candidates[0][0])

    if selected is None:
        # fallback to whole page text
        text = soup.get_text(separator="\n")
    else:
        text = selected.get_text(separator="\n")

    # Remove common accessibility boilerplate that often appears site-wide
    text = re.sub(r"(?i)skip\s+to\s+main\s+content", "", text)
    # Remove repetitive 'Menu' or short navigation-only headings left alone on lines
    text = re.sub(r"^\s*(Menu|Skip To Main Content|Search)\s*$", "", text, flags=re.M)

    # collapse multiple blank lines
    text = re.sub(r"\n\s+\n+", "\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text_from_pdf_bytes(b: bytes) -> str:
    if pdf_extract_text is None:
        raise RuntimeError("pdfminer.six is required to extract PDF text")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
        f.write(b)
        tmp = f.name
    try:
        txt = pdf_extract_text(tmp)
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass
    return txt or ""


def write_scraped_file(filename: Path, text: str):
    filename.write_text(text, encoding="utf-8")


def url_segment_text(url: str, id_: str, content: str) -> str:
    return f"{SEG_START.format(url=url, id=id_)}\n{content}\n{SEG_END.format(url=url, id=id_)}\n\n"


def generate_pdf_from_txt(txt_path: Path, pdf_path: Path):
    if canvas is None:
        logger.warning("reportlab not installed; skipping PDF generation")
        return
    text = txt_path.read_text(encoding="utf-8")
    c = canvas.Canvas(str(pdf_path), pagesize=LETTER)
    width, height = LETTER
    margin = 40
    y = height - margin
    line_height = 12
    for paragraph in text.splitlines():
        # wrap long lines
        if not paragraph:
            y -= line_height
            if y < margin:
                c.showPage()
                y = height - margin
            continue
        words = paragraph.split()
        line = ""
        for w in words:
            if c.stringWidth(line + " " + w) > (width - 2 * margin):
                c.drawString(margin, y, line)
                y -= line_height
                line = w
                if y < margin:
                    c.showPage()
                    y = height - margin
            else:
                line = (line + " " + w).strip()
        if line:
            c.drawString(margin, y, line)
            y -= line_height
            if y < margin:
                c.showPage()
                y = height - margin
    c.save()


def update_combined_file(meta, urls):
    # Rebuild combined file from current per-URL .txt files in the same
    # order as `urls`. This simplifies incremental behavior: removed URLs
    # simply don't appear; new/updated files appear in order.
    segments = []
    for url in urls:
        entry = meta.get(url)
        if not entry:
            # file may exist even without metadata (edge-case)
            fname = f"{meta_filename_from_url(url)}.txt"
        else:
            fname = entry.get("filename")
        fpath = SCRAPED_DIR / fname
        content = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
        id_ = entry.get("id") if entry else sha1(url)
        segments.append(url_segment_text(url, id_, content))

    new_text = "\n\n".join(segments).strip() + "\n"
    new_text = re.sub(r"\n{3,}", "\n\n", new_text)
    COMBINED_TXT.write_text(new_text, encoding="utf-8")
    try:
        generate_pdf_from_txt(COMBINED_TXT, COMBINED_PDF)
    except Exception as e:
        logger.warning("PDF generation failed: %s", e)


def meta_filename_from_url(url: str) -> str:
    return sha1(url)


def existing_urls_from_file(path: Path):
    if not path.exists():
        return []
    urls = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = urlparse(line)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            logger.debug("Ignoring non-HTTP URL entry in %s: %s", path, line)
            continue
        urls.append(line)
    return urls


def has_complete_scraped_cache(urls):
    if not urls:
        return False
    required = {f"{meta_filename_from_url(u)}.txt" for u in urls}
    existing = {p.name for p in SCRAPED_DIR.glob("*.txt")}
    return required.issubset(existing)


def main():
    setup_logging()
    ensure_dirs()
    urls = existing_urls_from_file(URLS_FILE)
    if urls and has_complete_scraped_cache(urls):
        logger.info("All current URLs already have scraped output. Skipping pre-scrape validation and re-scrape work.")
    else:
        # Run pre-scrape pipeline steps: generate workbook then filter and produce clgUrls.txt
        try:
            runner = str(BASE_DIR / "university_agent_runner.py")
            filterer = str(BASE_DIR / "filter_and_write_clgurls.py")
            logger.info("Running pre-scrape agent: %s", runner)
            subprocess.check_call([sys.executable, runner], cwd=str(BASE_DIR))
            logger.info("Running pre-scrape filter: %s", filterer)
            subprocess.check_call([sys.executable, filterer], cwd=str(BASE_DIR))
        except subprocess.CalledProcessError as e:
            logger.exception("Pre-scrape step failed: %s", e)
        urls = existing_urls_from_file(URLS_FILE)
    # Build in-memory meta for existing scraped files. We do not persist metadata.
    meta = {}
    for url in urls:
        fname = f"{meta_filename_from_url(url)}.txt"
        fpath = SCRAPED_DIR / fname
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8")
            except Exception:
                content = ""
            meta[url] = {"id": sha1(url), "filename": fname, "hash": sha1(content)}

    session = requests.Session()
    session.headers.update({"User-Agent": "URLScrapePipeline/1.0 (+https://example.local)"})

    # Delete scraped files for URLs removed from the list.
    # We determine files in the scraped directory whose sha1 filenames do not
    # correspond to any current URL and remove them.
    current_fnames = {f"{meta_filename_from_url(u)}.txt" for u in urls}
    for f in SCRAPED_DIR.glob("*.txt"):
        if f.name not in current_fnames:
            try:
                f.unlink()
                logger.info("Removed scraped file no longer referenced: %s", f.name)
            except Exception:
                logger.exception("Failed to remove old scraped file: %s", f.name)

    for url in urls:
        m = meta.get(url, {})
        filename = m.get("filename") or f"{meta_filename_from_url(url)}.txt"
        fpath = SCRAPED_DIR / filename

        if fpath.exists():
            logger.info("Skipping already scraped URL: %s -> %s", url, fpath)
            persist_urls(urls)
            continue

        logger.info("Processing: %s", url)

        # If we have metadata with caching headers, do a conditional GET to detect updates.
        headers = {}
        if m:
            if m.get("etag"):
                headers['If-None-Match'] = m.get('etag')
            if m.get('last_modified'):
                headers['If-Modified-Since'] = m.get('last_modified')

        try:
            resp = session.get(url, headers=headers, allow_redirects=True, timeout=30)
        except Exception as e:
            logger.error("Fetch failed for %s: %s", url, e)
            persist_urls(urls)
            continue

        # 304 Not Modified -> skip
        if resp.status_code == 304:
            logger.info("Not modified (304), skipping: %s", url)
            persist_urls(urls)
            continue

        # If server returned content, process it.
        if is_pdf_response(resp) or str(url).lower().endswith('.pdf'):
            try:
                txt = extract_text_from_pdf_bytes(resp.content)
            except Exception as e:
                logger.error("PDF text extraction failed for %s: %s", url, e)
                txt = ""
            write_scraped_file(fpath, txt)
            content_hash = sha1(txt)
            meta[url] = {"id": sha1(url), "filename": filename, "hash": content_hash, "etag": resp.headers.get("ETag"), "last_modified": resp.headers.get("Last-Modified")}
            logger.info("Saved PDF-derived text for %s -> %s", url, fpath)
        else:
            html = resp.text or ''
            txt = extract_text_from_html(html)
            write_scraped_file(fpath, txt)
            content_hash = sha1(txt)
            meta[url] = {"id": sha1(url), "filename": filename, "hash": content_hash, "etag": resp.headers.get("ETag"), "last_modified": resp.headers.get("Last-Modified")}
            logger.info("Saved scraped text for %s -> %s", url, fpath)

        persist_urls(urls)

    # Rebuild combined outputs from current URLs
    update_combined_file(meta, urls)

    # Summary: check which URLs have corresponding scraped files
    total = len(urls)
    have = 0
    missing_urls = []
    for url in urls:
        fname = f"{meta_filename_from_url(url)}.txt"
        if (SCRAPED_DIR / fname).exists():
            have += 1
        else:
            missing_urls.append(url)

    logger.info("Done. Scraped texts in: %s", SCRAPED_DIR)
    logger.info("Combined outputs in: %s", COMBINED_DIR)
    logger.info("Summary: %d/%d URLs have scraped .txt files", have, total)
    if missing_urls:
        logger.warning("Missing scraped files for %d URLs. First few: %s", len(missing_urls), missing_urls[:5])


if __name__ == '__main__':
    main()
