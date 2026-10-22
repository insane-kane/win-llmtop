#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run CI's lint check (ruff, at the pinned version) in the local test suite.

	Nothing is installed: ruff runs as a throwaway tool at the exact version
	pinned in .github/requirements/lint.txt. The choice of ruff is made in one
	small function, find_ruff, which probes candidates in order -- uvx (offline),
	the current interpreter's ruff, then a ruff on PATH -- with --version, and
	uses the first that reports the pin. A wrong version, or no ruff at all,
	prints a NOTE and does not fail; a real lint failure prints ruff's output
	and fails. The project's own configuration decides, as in CI: exactly
	`check .`, no other flags.

	python -I tools/win-llmtop/tests/test_lint.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINT_TXT = os.path.join(ROOT, ".github", "requirements", "lint.txt")
TIMEOUT = 120


def parse_pin(lint_txt_path):
	"""Return the ruff version pinned in a lint.txt, or None if there is no pin line."""
	try:
		with open(lint_txt_path, encoding="utf-8") as f:
			text = f.read()
	except OSError:
		return None
	for line in text.splitlines():
		m = re.search(r"ruff==([0-9][0-9A-Za-z.]*)", line)
		if m:
			return m.group(1)
	return None


def probe_version(cmd):
	"""Run `cmd --version`; return the reported version, or None if it fails or prints none."""
	try:
		proc = subprocess.run(list(cmd) + ["--version"], capture_output=True, text=True, timeout=30)
	except (OSError, subprocess.SubprocessError):
		return None
	if proc.returncode != 0:
		return None
	m = re.search(r"([0-9]+\.[0-9]+\.[0-9]+)", proc.stdout)
	return m.group(1) if m else None


def _uvx_prefixes():
	"""Command prefixes for uvx, in order: PATH, then the user's home install."""
	prefixes = []
	which = shutil.which("uvx")
	if which:
		prefixes.append([which])
	home = os.path.expanduser("~")
	for name in ("uvx", "uvx.exe"):
		p = os.path.join(home, ".local", "bin", name)
		if os.path.exists(p):
			prefixes.append([p])
	return prefixes


def find_ruff(pin, uvx_cmds=None, py_ruff_cmd=None, ruff_cmd=None):
	"""Pick a ruff command that reports exactly `pin`.

	Returns (cmd, note): cmd is the command to run `check .` with, or None when no
	pinned ruff is available; note is the NOTE line to print, or None.
	"""
	candidates = []
	for prefix in (uvx_cmds if uvx_cmds is not None else _uvx_prefixes()):
		candidates.append((True, list(prefix) + ["--offline", "ruff@" + pin]))
	candidates.append((False, list(py_ruff_cmd if py_ruff_cmd is not None else [sys.executable, "-m", "ruff"])))
	if ruff_cmd is None:
		w = shutil.which("ruff")
		ruff_cmd = [w] if w else None
	if ruff_cmd:
		candidates.append((True, list(ruff_cmd)))

	wrong = None
	for is_ac, cmd in candidates:
		v = probe_version(cmd)
		if v == pin:
			return cmd, None
		if v is not None and is_ac and wrong is None:
			wrong = v
	if wrong is not None:
		return None, "NOTE ruff %s found, CI uses %s: lint was NOT checked" % (wrong, pin)
	return None, "NOTE ruff %s is not available: lint was NOT checked (run: uvx ruff@%s --version once, with network)" % (pin, pin)


def run_check(cmd, project_dir):
	"""Run exactly `check .` in project_dir; return (rc, output). A timeout is a failure."""
	try:
		proc = subprocess.run(list(cmd) + ["check", "."], capture_output=True, text=True, cwd=project_dir, timeout=TIMEOUT)
	except subprocess.TimeoutExpired:
		return 124, "ruff check timed out after %d s" % TIMEOUT
	except OSError as e:
		return 125, "could not run ruff: %s" % e
	out = proc.stdout or ""
	if proc.stderr:
		out += proc.stderr
	return proc.returncode, out


def _write_fake(dirpath, name, version, check_rc=0, check_out=""):
	"""Write a tiny fake ruff script; return its path. `--version` prints `version`; `check .` writes `check_out` and exits `check_rc`."""
	path = os.path.join(dirpath, name)
	script = (
		"import sys\n"
		"args = sys.argv[1:]\n"
		"if '--version' in args:\n"
		"\tprint(%r)\n"
		"\tsys.exit(0)\n"
		"if 'check' in args:\n"
		"\tsys.stdout.write(%r)\n"
		"\tsys.exit(%d)\n"
		"sys.exit(2)\n"
	) % (version, check_out, check_rc)
	with open(path, "w", encoding="utf-8") as f:
		f.write(script)
	return path


def t_pin_parser():
	d = tempfile.mkdtemp()
	try:
		p = os.path.join(d, "lint.txt")
		with open(p, "w", encoding="utf-8") as f:
			f.write("PyYAML==6.0.3 \\\n--hash=sha256:abc \nruff==9.9.9 \\\n--hash=sha256:def \n")
		assert parse_pin(p) == "9.9.9"
	finally:
		shutil.rmtree(d, ignore_errors=True)


def t_pin_parser_missing():
	d = tempfile.mkdtemp()
	try:
		p = os.path.join(d, "lint.txt")
		with open(p, "w", encoding="utf-8") as f:
			f.write("PyYAML==6.0.3 \\\n--hash=sha256:abc \n")
		assert parse_pin(p) is None
		assert parse_pin(os.path.join(d, "absent.txt")) is None
	finally:
		shutil.rmtree(d, ignore_errors=True)


def t_probe_order():
	# uvx (a) is chosen over the interpreter's ruff (b) and a ruff on PATH (c).
	d = tempfile.mkdtemp()
	try:
		pin = "9.9.9"
		a = _write_fake(d, "a.py", "ruff " + pin)
		b = _write_fake(d, "b.py", "ruff " + pin)
		c = _write_fake(d, "c.py", "ruff " + pin)
		cmd, note = find_ruff(pin, uvx_cmds=[[sys.executable, a]], py_ruff_cmd=[sys.executable, b], ruff_cmd=[sys.executable, c])
		assert note is None, note
		assert cmd[1] == a, cmd
	finally:
		shutil.rmtree(d, ignore_errors=True)


def t_probe_order_b_before_c():
	# With no uvx, the interpreter's ruff (b) is chosen before a ruff on PATH (c).
	d = tempfile.mkdtemp()
	try:
		pin = "9.9.9"
		b = _write_fake(d, "b.py", "ruff " + pin)
		c = _write_fake(d, "c.py", "ruff " + pin)
		cmd, note = find_ruff(pin, uvx_cmds=[], py_ruff_cmd=[sys.executable, b], ruff_cmd=[sys.executable, c])
		assert note is None, note
		assert cmd[1] == b, cmd
	finally:
		shutil.rmtree(d, ignore_errors=True)


def t_not_available_note():
	d = tempfile.mkdtemp()
	try:
		pin = "9.9.9"
		a = _write_fake(d, "a.py", None)  # exits non-zero on --version
		cmd, note = find_ruff(pin, uvx_cmds=[[sys.executable, a]], py_ruff_cmd=[sys.executable, "-m", "no_such_module_zz"], ruff_cmd=None)
		assert cmd is None, cmd
		assert note == "NOTE ruff %s is not available: lint was NOT checked (run: uvx ruff@%s --version once, with network)" % (pin, pin), note
	finally:
		shutil.rmtree(d, ignore_errors=True)


def t_wrong_version_note():
	d = tempfile.mkdtemp()
	try:
		pin = "9.9.9"
		a = _write_fake(d, "a.py", "ruff 0.0.1")  # a reports a different version
		cmd, note = find_ruff(pin, uvx_cmds=[[sys.executable, a]], py_ruff_cmd=[sys.executable, "-m", "no_such_module_zz"], ruff_cmd=None)
		assert cmd is None, cmd
		assert note == "NOTE ruff 0.0.1 found, CI uses %s: lint was NOT checked" % pin, note
	finally:
		shutil.rmtree(d, ignore_errors=True)


def t_failing_check():
	d = tempfile.mkdtemp()
	try:
		out = "tests/test_cli.py:462:10: B905 zip() without strict=\nFound 1 error.\n"
		fake = _write_fake(d, "ruff.py", "ruff 9.9.9", check_rc=1, check_out=out)
		rc, got = run_check([sys.executable, fake], d)
		assert rc == 1, rc
		assert "B905" in got, got
	finally:
		shutil.rmtree(d, ignore_errors=True)


SELF_TESTS = [
	("pin_parser", t_pin_parser),
	("pin_parser_missing", t_pin_parser_missing),
	("probe_order", t_probe_order),
	("probe_order_b_before_c", t_probe_order_b_before_c),
	("not_available_note", t_not_available_note),
	("wrong_version_note", t_wrong_version_note),
	("failing_check", t_failing_check),
]


def main():
	failed = 0
	for name, fn in SELF_TESTS:
		try:
			fn()
		except Exception as e:
			failed += 1
			print("FAIL %s: %s: %s" % (name, type(e).__name__, e))
		else:
			print("PASS " + name)
	if failed:
		print("%d self-test case(s) failed" % failed)
		return 1
	pin = parse_pin(LINT_TXT)
	if pin is None:
		print("FAIL no ruff== pin found in %s" % LINT_TXT)
		return 1
	cmd, note = find_ruff(pin)
	if note is not None:
		print(note)
	else:
		rc, out = run_check(cmd, ROOT)
		if out.strip():
			sys.stdout.write(out if out.endswith("\n") else out + "\n")
		if rc != 0:
			print("FAIL ruff check exited %d" % rc)
			return 1
		print("PASS ruff check")
	print("ALL PASS")
	return 0


if __name__ == "__main__":
	sys.exit(main())
