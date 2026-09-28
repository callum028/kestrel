#!/usr/bin/env python3
"""A fake `claude` binary for tests.

Never runs the real CLI - that bills Callum's subscription. This stands in
for it: `KESTREL_FAKE_CLAUDE_MODE` picks a canned behaviour, and when a
response file is given (`KESTREL_FAKE_CLAUDE_RESPONSE`) its contents become
stdout for the "ok" mode. Every invocation's argv is appended, one JSON line
per call, to `KESTREL_FAKE_CLAUDE_ARGV_LOG` when set, so a test can assert on
exactly what `BrainRunner` constructed without mocking the subprocess call
itself.
"""

from __future__ import annotations

import json
import os
import sys
import time

argv_log = os.environ.get("KESTREL_FAKE_CLAUDE_ARGV_LOG")
if argv_log:
    with open(argv_log, "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\n")

mode = os.environ.get("KESTREL_FAKE_CLAUDE_MODE", "ok")

if mode == "timeout":
    time.sleep(30)
    sys.exit(0)

if mode == "fail":
    sys.stderr.write("simulated claude failure\n")
    sys.exit(1)

if mode == "garbage":
    sys.stdout.write("this is not json")
    sys.exit(0)

response_path = os.environ.get("KESTREL_FAKE_CLAUDE_RESPONSE")
if response_path:
    with open(response_path) as f:
        sys.stdout.write(f.read())
else:
    sys.stdout.write(json.dumps({"result": "canned response"}))
sys.exit(0)
