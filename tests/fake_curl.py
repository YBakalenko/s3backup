#!/usr/bin/env python3
"""Fetch installer fixtures locally; never make network requests."""

import json
import os
from pathlib import Path
import shutil
import sys


args = sys.argv[1:]
url = args[-1]
allowed = "https://raw.githubusercontent.com/YBakalenko/s3backup/main/"
name = url.removeprefix(allowed)
if not url.startswith(allowed) or name not in ("s3backup", "S98s3backup"):
    raise SystemExit(97)
destination = Path(args[args.index("--output") + 1])
with open(os.environ["FAKE_CURL_LOG"], "a", encoding="utf-8") as out:
    out.write(json.dumps({"argv": args, "url": url, "destination": str(destination)}) + "\n")
if os.environ.get("FAKE_CURL_FAIL") == name:
    destination.write_text("partial failed HTTP response")
    raise SystemExit(22)
shutil.copyfile(Path(os.environ["FAKE_CURL_SOURCE"]) / name, destination)
