#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Docs-coverage tests for win-llmtop.

	Every command-line option and environment variable must be documented in both
	README.md and docs/llmtop.1.md. The man page's Alerts section must name every
	threshold that appears in an alert condition — every numeric CFG key referenced
	in Alerts.evaluate in llmtop_lib/state.py, the method that computes every alert
	condition and message. Keys that only set timing or non-alert behaviour (the
	hysteresis clear_s in Alerts.sweep, the sampling rate hz, the history span
	history_hours) are not conditions, so they are not required there; when such a
	name is not in the man page at all the test prints it as a note, not a failure.
	A threshold name counts as named when it appears as a code span of its own
	(`name`), the convention both docs use for names.

	python -I tools/win-llmtop/tests/test_docs.py
"""

import ast
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llmtop_lib import state  # noqa: E402
from tests import _public_names  # noqa: E402

README = (ROOT / "README.md").read_text(encoding="utf-8")
MAN = (ROOT / "docs" / "llmtop.1.md").read_text(encoding="utf-8")
LLMTOP_PY = str(ROOT / "llmtop.py")
FORBIDDEN = _public_names.FORBIDDEN


def help_text():
	proc = subprocess.run([sys.executable, LLMTOP_PY, "--help"], capture_output=True, text=True, encoding="utf-8")
	if proc.returncode != 0:
		raise AssertionError("llmtop.py --help exited %d: %s" % (proc.returncode, proc.stderr.strip()))
	return proc.stdout


def options():
	return sorted(set(re.findall(r"--[a-z][a-z-]*", help_text())) | {"-h"})


def env_vars():
	src = (ROOT / "llmtop.py").read_text(encoding="utf-8")
	return sorted(set(re.findall(r"LLMTOP_[A-Z_]+|LLAMA_API_KEY", src)))


def numeric_thresholds():
	"""The --threshold names: the numeric keys of state.CFG, as llmtop.py computes them."""
	return {k for k, v in state.CFG.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def alert_condition_thresholds():
	"""The numeric CFG keys referenced in Alerts.evaluate, where every alert condition and message is computed."""
	src = (ROOT / "llmtop_lib" / "state.py").read_text(encoding="utf-8")
	tree = ast.parse(src)
	cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Alerts")
	fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "evaluate")
	keys = {n.slice.value for n in ast.walk(fn) if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)}
	return keys & numeric_thresholds()


def named(text, name):
	return re.search(r"`%s`" % re.escape(name), text) is not None


def alerts_section():
	m = re.search(r"^### Alerts\n.*?(?=^### |\Z)", MAN, re.M | re.S)
	if not m:
		raise AssertionError("no ### Alerts section in docs/llmtop.1.md")
	return m.group(0)


def test_options_in_readme_and_man():
	missing = []
	for opt in options():
		if opt not in README:
			missing.append("%s in README.md" % opt)
		if opt not in MAN:
			missing.append("%s in docs/llmtop.1.md" % opt)
	if missing:
		raise AssertionError("not documented: " + ", ".join(missing))


def test_env_vars_in_readme_and_man():
	missing = []
	for var in env_vars():
		if var not in README:
			missing.append("%s in README.md" % var)
		if var not in MAN:
			missing.append("%s in docs/llmtop.1.md" % var)
	if missing:
		raise AssertionError("not documented: " + ", ".join(missing))


def test_threshold_names_in_man():
	absent = sorted(k for k in numeric_thresholds() if not named(MAN, k))
	if absent:
		print("note: --threshold names not in docs/llmtop.1.md: " + ", ".join(absent))
	section = alerts_section()
	missing = sorted(k for k in alert_condition_thresholds() if not named(section, k))
	if missing:
		raise AssertionError("Alerts section names fewer thresholds than it should: " + ", ".join(missing))


def test_no_local_names():
	bad = []
	for name in FORBIDDEN:
		if name.lower() in README.lower():
			bad.append("%s in README.md" % name)
		if name.lower() in MAN.lower():
			bad.append("%s in docs/llmtop.1.md" % name)
	if bad:
		raise AssertionError("found: " + ", ".join(bad))


CASES = [
	("options_in_readme_and_man", test_options_in_readme_and_man),
	("env_vars_in_readme_and_man", test_env_vars_in_readme_and_man),
	("threshold_names_in_man", test_threshold_names_in_man),
	("no_local_names", test_no_local_names),
]


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
