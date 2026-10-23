#!/usr/bin/env python3
"""
Run the full pipeline in order:
  1. university_agent_runner.py
  2. filter_and_write_clgurls.py
  3. scrape_pipeline.py

This script forwards common args to the underlying steps and provides
skip flags for development.
"""
import argparse
import logging
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).parent


def run_script(script: str, args: list[str] | None = None) -> None:
    cmd = [sys.executable, str(BASE_DIR / script)]
    if args:
        cmd += args
    logging.info("Running: %s %s", script, " ".join(args or []))
    subprocess.check_call(cmd, cwd=str(BASE_DIR))


def parse_args():
    p = argparse.ArgumentParser(description="Orchestrate the full URL scrape pipeline")
    p.add_argument("--input", default="UniversityNames.xlsx", help="Input Excel for university names")
    p.add_argument("--output", default="University_Output1.xlsx", help="Output Excel workbook path")
    p.add_argument("--model", default=None, help="LLM model name (overrides OPENAI_MODEL env var)")
    p.add_argument("--timeout", type=int, default=5, help="Timeout seconds for URL reachability checks")
    p.add_argument("--skip-agent", action="store_true", help="Skip university_agent_runner step")
    p.add_argument("--skip-filter", action="store_true", help="Skip filter_and_write_clgurls step")
    p.add_argument("--skip-scrape", action="store_true", help="Skip scrape_pipeline step")
    return p.parse_args()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    args = parse_args()

    agent_args = ["--input", args.input, "--output", args.output]
    if args.model:
        agent_args += ["--model", args.model]

    filter_args = ["--input", args.output, "--out", "clgUrls.txt", "--timeout", str(args.timeout)]

    try:
        if not args.skip_agent:
            run_script("university_agent_runner.py", agent_args)

        if not args.skip_filter:
            run_script("filter_and_write_clgurls.py", filter_args)

        if not args.skip_scrape:
            run_script("scrape_pipeline.py", None)

    except subprocess.CalledProcessError as e:
        logging.exception("Pipeline step failed: %s", e)
        sys.exit(e.returncode or 1)


if __name__ == "__main__":
    main()
