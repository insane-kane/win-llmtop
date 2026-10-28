#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Aaron Kane <software@kanenet.us>
"""Docs-coverage and link hygiene tests for win-llmtop.

	Every command-line option and environment variable must be documented in
	docs/llmtop.1.md (the complete reference), and every option named across
	README.md and docs/*.md must be a real option in --help (or a known flag of
	an external tool such as llama-server, pip, or gh). Every relative link in
	docs/*.md must resolve to an existing file and heading anchor; every link in
	README.md must be an absolute https:// URL or a #anchor for PyPI rendering.
	The man page's Alerts section must name every threshold that appears in an
	alert condition in Alerts.evaluate in llmtop_lib/state.py. No page may
	contain forbidden local names or email addresses.

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

README_PATH = ROOT / "README.md"
MAN_PATH = ROOT / "docs" / "llmtop.1.md"
EXPECTED_DOCS = ("README.md", "install.md", "usage.md", "troubleshooting.md", "llmtop.1.md")
README = README_PATH.read_text(encoding="utf-8")
MAN = MAN_PATH.read_text(encoding="utf-8")
LLMTOP_PY = str(ROOT / "llmtop.py")
FORBIDDEN = _public_names.FORBIDDEN

# Flags belonging to external tools explicitly discussed in the documentation (llama-server, llama-fit-params, pip, gh)
EXTERNAL_FLAGS = {
	"--batch-size",
	"--cpu-moe",
	"--host",
	"--log-file",
	"--log-timestamps",
	"--n-cpu-moe",
	"--n-gpu-layers",
	"--no-host",
	"--no-slots",
	"--override-tensor",
	"--parallel",
	"--port",
	"--repo",
	"--slots",
	"--upgrade",
	"--user",
}

RE_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
RE_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)
RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def doc_pages():
	"""All Markdown documentation files: README.md plus every file in docs/*.md."""
	pages = [README_PATH] + sorted((ROOT / "docs").glob("*.md"))
	return [(p.relative_to(ROOT).as_posix(), p, p.read_text(encoding="utf-8")) for p in pages]


def github_slug(heading_text):
	"""GitHub's heading anchor rule: lowercase, punctuation dropped, spaces to '-'."""
	cleaned = re.sub(r"[^\w\s-]", "", heading_text.lower()).strip()
	return cleaned.replace(" ", "-")


def heading_slugs(path):
	text = path.read_text(encoding="utf-8")
	slugs = set()
	counts = {}
	for m in RE_HEADING.finditer(text):
		base = github_slug(m.group(1))
		if not base:
			continue
		if base not in counts:
			counts[base] = 0
			slugs.add(base)
		else:
			counts[base] += 1
			slugs.add("%s-%d" % (base, counts[base]))
	return slugs


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


def test_doc_set_exists():
	missing = [name for name in EXPECTED_DOCS if not (ROOT / "docs" / name).is_file()]
	if missing:
		raise AssertionError("missing docs files: " + ", ".join(missing))


def test_options_in_man():
	missing = [opt for opt in options() if opt not in MAN]
	if missing:
		raise AssertionError("not documented in docs/llmtop.1.md: " + ", ".join(missing))


def test_env_vars_in_man():
	missing = [var for var in env_vars() if var not in MAN]
	if missing:
		raise AssertionError("not documented in docs/llmtop.1.md: " + ", ".join(missing))


def test_named_options_in_docs():
	real = set(options())
	allowed = real | EXTERNAL_FLAGS
	bad = []
	for rel, _path, text in doc_pages():
		# Strip link targets so URLs or #anchors containing '--' are not treated as CLI flags
		prose = RE_LINK.sub(lambda m: m.group(0).split("](")[0] + "]()", text)
		for flag in sorted(set(re.findall(r"--[a-z][a-z0-9-]*", prose))):
			if flag not in allowed:
				bad.append("%s in %s" % (flag, rel))
		# Any option linked directly to llmtop.1.md#options must be a win-llmtop option
		for m in re.finditer(r"\[`(--[a-z][a-z0-9-]*)`\]\([^)]*llmtop\.1\.md#options\)", text):
			if m.group(1) not in real:
				bad.append("%s (linked to man page) in %s" % (m.group(1), rel))
	if bad:
		raise AssertionError("unknown or stale options: " + ", ".join(bad))


def test_readme_links_for_pypi():
	bad = []
	slugs = heading_slugs(README_PATH)
	blob_prefix = "https://github.com/insane-kane/win-llmtop/blob/main/"
	for m in RE_LINK.finditer(README):
		target = m.group(1).strip()
		if target.startswith("#"):
			if target[1:] not in slugs:
				bad.append("broken anchor %s in README.md" % target)
		elif not target.startswith("https://"):
			bad.append("non-absolute link %s in README.md" % target)
		elif target.startswith(blob_prefix) and "#" in target:
			rel_path, _, anchor = target[len(blob_prefix):].partition("#")
			target_file = (ROOT / rel_path).resolve()
			if target_file.is_file() and target_file.suffix.lower() == ".md":
				if anchor not in heading_slugs(target_file):
					bad.append("broken anchor #%s in %s (from README.md)" % (anchor, rel_path))
	if bad:
		raise AssertionError("; ".join(bad))


def test_docs_relative_links_resolve():
	bad = []
	for rel, path, text in doc_pages():
		if path == README_PATH:
			continue
		for m in RE_LINK.finditer(text):
			target = m.group(1).strip()
			if target.startswith(("https://", "http://")):
				continue
			file_part, _, anchor = target.partition("#")
			target_file = path if not file_part else (path.parent / file_part).resolve()
			try:
				target_file.relative_to(ROOT)
			except ValueError:
				bad.append("%s in %s escapes repo root" % (target, rel))
				continue
			if not target_file.is_file():
				bad.append("%s in %s -> missing file" % (target, rel))
				continue
			if anchor and target_file.suffix.lower() == ".md":
				if anchor not in heading_slugs(target_file):
					bad.append("%s in %s -> missing heading #%s" % (target, rel, anchor))
	if bad:
		raise AssertionError("; ".join(bad))


def test_threshold_names_in_man():
	absent = sorted(k for k in numeric_thresholds() if not named(MAN, k))
	if absent:
		print("note: --threshold names not in docs/llmtop.1.md: " + ", ".join(absent))
	section = alerts_section()
	missing = sorted(k for k in alert_condition_thresholds() if not named(section, k))
	if missing:
		raise AssertionError("Alerts section names fewer thresholds than it should: " + ", ".join(missing))


def test_no_local_names_or_emails():
	bad = []
	for rel, _path, text in doc_pages():
		low = text.lower()
		for name in FORBIDDEN:
			if name.lower() in low:
				bad.append("%s in %s" % (name, rel))
		for m in RE_EMAIL.finditer(text):
			bad.append("email %s in %s" % (m.group(0), rel))
	if bad:
		raise AssertionError("found: " + ", ".join(bad))


CASES = [
	("doc_set_exists", test_doc_set_exists),
	("options_in_man", test_options_in_man),
	("env_vars_in_man", test_env_vars_in_man),
	("named_options_in_docs", test_named_options_in_docs),
	("readme_links_for_pypi", test_readme_links_for_pypi),
	("docs_relative_links_resolve", test_docs_relative_links_resolve),
	("threshold_names_in_man", test_threshold_names_in_man),
	("no_local_names_or_emails", test_no_local_names_or_emails),
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
