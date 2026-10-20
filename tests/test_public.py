#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Public-tree hygiene tests for win-llmtop.

	Every tracked text file under the folder, except LICENSE, must be free of
	local LLM drive paths and local host or model names.

	python -I tools/win-llmtop/tests/test_public.py
"""

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import _public_names  # noqa: E402


def tracked_files():
	try:
		proc = subprocess.run(
			["git", "ls-files", "--cached", "--others", "--exclude-standard"],
			cwd=str(ROOT),
			capture_output=True,
			text=True,
			encoding="utf-8",
			timeout=10,
		)
		if proc.returncode == 0 and proc.stdout.strip():
			return sorted({
				ROOT / rel
				for rel in proc.stdout.splitlines()
				if rel and rel != "LICENSE" and (ROOT / rel).is_file()
			})
	except (OSError, subprocess.SubprocessError):
		pass
	skip_dirs = {".git", "__pycache__", "build", "dist", ".venv", "venv"}
	out = []
	for dirpath, dirnames, filenames in os.walk(ROOT):
		dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.endswith(".egg-info")]
		for fn in filenames:
			p = pathlib.Path(dirpath) / fn
			rel = p.relative_to(ROOT).as_posix()
			if rel != "LICENSE" and not fn.endswith(".pyc") and not fn.startswith(".coverage"):
				out.append(p)
	return sorted(out)


def check_pattern(rx):
	hits = []
	for path in tracked_files():
		try:
			raw = path.read_bytes()
		except OSError:
			continue
		if b"\x00" in raw:
			continue
		text = raw.decode("utf-8", errors="replace")
		rel = path.relative_to(ROOT).as_posix()
		for lineno, line in enumerate(text.splitlines(), 1):
			if rx.search(line):
				hits.append("%s:%d" % (rel, lineno))
	if hits:
		raise AssertionError("matched in " + ", ".join(hits))


CASES = [(label, lambda rx=rx: check_pattern(rx)) for label, rx in _public_names.PATTERNS]


def main():
	failed = 0
	for name, fn in CASES:
		try:
			fn()
		except Exception as e:
			failed += 1
			print("FAIL %s: %s: %s" % (name, type(e).__name__, e))
		else:
			print("PASS " + name)
	if failed:
		print("%d of %d cases failed" % (failed, len(CASES)))
		sys.exit(1)
	print("ALL PASS")


if __name__ == "__main__":
	main()
