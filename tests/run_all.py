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
	"""Stream the suite's output; return (exit code, saw ALL PASS)."""
	proc = subprocess.Popen([sys.executable, "-I", path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
		text=True, encoding="utf-8", errors="replace")
	saw_pass = False
	assert proc.stdout is not None
	for line in proc.stdout:
		sys.stdout.write(line)
		if line.strip() == "ALL PASS":
			saw_pass = True
	proc.wait()
	return proc.returncode, saw_pass


def main():
	files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
	if not files:
		print("no test suites found in %s" % HERE)
		return 1
	passed = 0
	failed = 0
	for path in files:
		name = os.path.basename(path)
		print("\n=== %s ===" % name, flush=True)
		rc, saw_pass = run_suite(path)
		if rc == 0 and saw_pass:
			passed += 1
		else:
			failed += 1
	print("\n%d suite(s) passed, %d failed" % (passed, failed))
	return 1 if failed else 0


if __name__ == "__main__":
	sys.exit(main())
