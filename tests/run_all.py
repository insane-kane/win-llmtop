#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run every tests/test_*.py as its own process and summarise the result.

	Each suite is started with `sys.executable -I <file>`, its output is streamed
	as it arrives, and a suite counts as failed if it exits non-zero or prints no
	`ALL PASS` line. Works from any current directory.

	python -I tools/win-llmtop/tests/run_all.py
"""

import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run_suite(path):
	"""Stream the suite's output; return (exit code, saw ALL PASS, NOTE lines)."""
	proc = subprocess.Popen([sys.executable, "-I", path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
		text=True, encoding="utf-8", errors="replace")
	saw_pass = False
	notes = []
	assert proc.stdout is not None
	for line in proc.stdout:
		sys.stdout.write(line)
		if line.strip() == "ALL PASS":
			saw_pass = True
		if line.startswith("NOTE "):
			notes.append(line.rstrip("\n"))
	proc.wait()
	return proc.returncode, saw_pass, notes


def main():
	files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
	if not files:
		print("no test suites found in %s" % HERE)
		return 1
	passed = 0
	failed = 0
	notes = []
	for path in files:
		name = os.path.basename(path)
		print("\n=== %s ===" % name, flush=True)
		rc, saw_pass, suite_notes = run_suite(path)
		notes.extend(suite_notes)
		if rc == 0 and saw_pass:
			passed += 1
		else:
			failed += 1
	print("\n%d suite(s) passed, %d failed" % (passed, failed))
	print("notes:")
	for note in notes:
		print(note)
	return 1 if failed else 0


if __name__ == "__main__":
	sys.exit(main())
