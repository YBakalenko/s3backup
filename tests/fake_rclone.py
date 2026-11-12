#!/usr/bin/env python3
"""An intentionally read-only S3 double; forbidden commands fail loudly.

No credentials or network are used. FAKE_S3_ROOT is a plain directory with
bucket/prefix/object paths. Tests can inject failure and latency through files
outside that tree, so downloaded bytes remain the exact fixture bytes.
"""

import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import time


def main():
    args = sys.argv[1:]
    if args in (["version"], ["--version"]):
        print("rclone v1.73.2-test")
        return 0
    commands = {"lsjson", "lsf", "cat", "copyto", "ls", "size"}
    command_index = next((i for i, value in enumerate(args) if value in commands), None)
    if command_index is None:
        print("FAKE_RCLONE_FORBIDDEN_COMMAND", file=sys.stderr)
        return 97
    command = args[command_index]
    tail = args[command_index + 1:]
    # Rclone accepts global options before or after its subcommand.
    positional = []
    options_with_values = {
        "--config", "--transfers", "--checkers", "--buffer-size",
        "--multi-thread-streams", "--retries", "--low-level-retries",
        "--retries-sleep", "--contimeout", "--timeout", "--bwlimit",
        "--log-file", "--log-level", "--max-depth", "--include", "--exclude",
        "--filter", "--format", "--s3-list-version", "--max-duration",
        "--max-buffer-memory", "--stats", "--user-agent", "--s3-provider",
        "--max-transfer", "--cutoff-mode",
    }
    i = 0
    while i < len(tail):
        arg = tail[i]
        if arg in options_with_values:
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            positional.append(arg)
            i += 1
    if not positional or ":" not in positional[0]:
        print("FAKE_RCLONE_SOURCE_MUST_BE_REMOTE", file=sys.stderr)
        return 96
    source = positional[0]
    root = Path(os.environ["FAKE_S3_ROOT"]).resolve()
    relative = source.split(":", 1)[1].lstrip("/")
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        print("FAKE_RCLONE_TRAVERSAL", file=sys.stderr)
        return 95
    calls = Path(os.environ["FAKE_RCLONE_CALLS"])
    record = {"command": command, "source": source, "argv": args, "pid": os.getpid()}
    if command == "copyto" and len(positional) > 1:
        record["destination"] = positional[1]
    with calls.open("a", encoding="utf-8") as out:
        out.write(json.dumps(record) + "\n")
    control_file = os.environ.get("FAKE_RCLONE_CONTROL")
    control = json.loads(Path(control_file).read_text()) if control_file and Path(control_file).exists() else {}
    if control.get("fail_all") or source in control.get("fail_sources", []):
        print("FAKE_RCLONE_NETWORK_FAILURE", file=sys.stderr)
        return 12
    if command == "copyto" and not relative.endswith(".json"):
        time.sleep(control.get("archive_delay_seconds", 0))
    if not path.exists():
        print("FAKE_RCLONE_NOT_FOUND", file=sys.stderr)
        return 3
    if command == "cat":
        sys.stdout.buffer.write(path.read_bytes())
    elif command == "copyto":
        if len(positional) != 2 or ":" in positional[1]:
            print("FAKE_RCLONE_DESTINATION_MUST_BE_LOCAL", file=sys.stderr)
            return 94
        dest = Path(positional[1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    else:
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        objects = []
        for file in files:
            name = file.name if path.is_file() else str(file.relative_to(path))
            objects.append({
                "Path": name, "Name": name, "Size": file.stat().st_size,
                "ModTime": datetime.datetime.fromtimestamp(file.stat().st_mtime, datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
                "IsDir": False,
            })
        if command == "lsjson":
            json.dump(objects, sys.stdout)
        elif command == "lsf":
            for obj in objects:
                print(obj["Path"])
        elif command == "ls":
            for obj in objects:
                print(obj["Size"], obj["Path"])
        else:
            json.dump({"count": len(objects), "bytes": sum(obj["Size"] for obj in objects)}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
