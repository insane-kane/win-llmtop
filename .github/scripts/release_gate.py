#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Aaron Kane <software@kanenet.us>
"""Release-gate check for win-llmtop.

	A commit whose version already has a tag is ordinary work: exit 0, and nothing
	is posted. Any other commit is a release attempt, and must be a new, unreleased
	version ready to fast-forward onto main, or this exits 1:
	1. pyproject.toml's project.version is X.Y.Z (three integers);
	2. CHANGELOG.md has '## [X.Y.Z] - YYYY-MM-DD' with a real calendar date;
	3. CITATION.cff's version equals project.version;
	4. no tag vX.Y.Z exists yet in the fetched git tags;
	5. X.Y.Z is strictly greater than every existing v* tag (as integer tuples);
	6. PyPI's JSON API returns HTTP 404 for win-llmtop X.Y.Z (fails closed on
	   HTTP 200 or any other status or network error).

	A commit that passes writes release=true to $GITHUB_OUTPUT, and the workflow then
	posts the release-gate check that main requires. No other commit gets that check.
"""

import datetime
import os
import pathlib
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
PYPROJECT_PATH = ROOT / "pyproject.toml"
CHANGELOG_PATH = ROOT / "CHANGELOG.md"
CITATION_PATH = ROOT / "CITATION.cff"
PYPI_PROJECT = "win-llmtop"
PYPI_URL_TEMPLATE = "https://pypi.org/pypi/%s/%s/json"
RE_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$", re.ASCII)


def parse_version(version):
	"""Parse an 'X.Y.Z' version string into an (X, Y, Z) int tuple, or return None."""
	if not isinstance(version, str):
		return None
	m = RE_VERSION.match(version)
	if not m:
		return None
	return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def changelog_pattern(version):
	"""Return the exact heading regex used by release.yml's verify job."""
	return r"^## \[" + re.escape(version) + r"\] - (\d{4}-\d{2}-\d{2})\s*$"


def parse_citation_version(citation_text):
	"""Extract the top-level version field from CITATION.cff text."""
	for line in citation_text.splitlines():
		m = re.match(r"^version:\s*(.+?)\s*$", line)
		if m:
			val = m.group(1).strip()
			if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
				val = val[1:-1]
			return val
	return None


def check_version_format(version):
	"""Check that pyproject.toml's project.version is X.Y.Z (three integers)."""
	if parse_version(version) is None:
		return "pyproject.toml project.version %r is not X.Y.Z (three integers)" % (version,)
	return None


def check_changelog(version, changelog_text):
	"""Check that CHANGELOG.md has '## [X.Y.Z] - YYYY-MM-DD' with a real calendar date."""
	m = re.search(changelog_pattern(version), changelog_text, re.M)
	if not m:
		return "CHANGELOG.md has no '## [%s] - YYYY-MM-DD' heading" % version
	date_str = m.group(1)
	try:
		datetime.date.fromisoformat(date_str)
	except ValueError as e:
		return "CHANGELOG.md heading for %s has invalid date %r: %s" % (version, date_str, e)
	return None


def check_citation(version, citation_text):
	"""Check that CITATION.cff's version equals project.version."""
	cff_version = parse_citation_version(citation_text)
	if cff_version is None:
		return "CITATION.cff has no top-level 'version:' field"
	if cff_version != version:
		return "CITATION.cff version %r does not equal pyproject.toml version %r" % (cff_version, version)
	return None


def check_tag_absent(version, tags):
	"""Check that no tag vX.Y.Z exists yet."""
	target = "v" + version
	if target in set(tags):
		return "tag %s already exists (fetched tags)" % target
	return None


def check_version_greater(version, tags):
	"""Check that X.Y.Z is greater than every existing v* tag, compared as integer tuples."""
	ver_tuple = parse_version(version)
	if ver_tuple is None:
		return "version %r is not X.Y.Z (three integers)" % (version,)
	v_tags = [t for t in tags if isinstance(t, str) and t.startswith("v")]
	bad_tags = []
	not_greater = []
	for tag in sorted(set(v_tags)):
		tag_ver = parse_version(tag[1:])
		if tag_ver is None:
			bad_tags.append(tag)
		elif not (ver_tuple > tag_ver):
			not_greater.append((tag_ver, tag))
	if bad_tags:
		return "existing v* tag(s) are not vX.Y.Z: %s" % ", ".join(bad_tags)
	if not_greater:
		highest_tag = max(not_greater)[1]
		return "version %s is not greater than existing tag %s" % (version, highest_tag)
	return None


def check_pypi(version, status_code, error=None):
	"""Check that PyPI has no such version (404 passes; 200 or any error fails closed)."""
	if error is not None:
		return "PyPI check failed for %s: %s" % (version, error)
	if status_code == 404:
		return None
	if status_code == 200:
		return "PyPI already has version %s (HTTP 200)" % version
	return "PyPI check failed for %s: unexpected HTTP status %r" % (version, status_code)


def evaluate(version, changelog_text, citation_text, tags, pypi_status, pypi_error=None):
	"""Run all six release-gate checks; return (ok, first_line, failures)."""
	ver_err = check_version_format(version)
	checks = [
		ver_err,
		check_changelog(version, changelog_text),
		check_citation(version, citation_text),
		check_tag_absent(version, tags),
		None if ver_err is not None else check_version_greater(version, tags),
		check_pypi(version, pypi_status, pypi_error),
	]
	failures = [err for err in checks if err is not None]
	if not failures:
		return True, "release-gate: %s is a new release" % version, []
	reason = "; ".join(failures)
	first_line = "release-gate: %s is not ready to release (%s)" % (version, reason)
	return False, first_line, failures


def is_released(version, tags):
	"""A version that already has its tag is not a release attempt: this is ordinary work on staging."""
	return "v" + version in set(tags)


def set_output(name, value):
	"""Pass a value to the workflow's next steps; does nothing outside Actions."""
	path = os.environ.get("GITHUB_OUTPUT")
	if path:
		with open(path, "a", encoding="utf-8") as f:
			f.write("%s=%s\n" % (name, value))


def read_pyproject_version(path=PYPROJECT_PATH):
	with open(path, "rb") as f:
		data = tomllib.load(f)
	return str(data["project"]["version"])


def get_fetched_tags(cwd=ROOT):
	"""Return the list of fetched tags from the local git repository (checkout uses fetch-depth: 0)."""
	proc = subprocess.run(
		["git", "tag", "--list", "v*"],
		cwd=str(cwd),
		capture_output=True,
		text=True,
		encoding="utf-8",
		timeout=15,
	)
	if proc.returncode != 0:
		raise RuntimeError("git tag --list failed (rc=%d): %s" % (proc.returncode, proc.stderr.strip()))
	return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def fetch_pypi_status(project, version, timeout=15):
	"""Query PyPI's JSON API for a specific version; return (status_code, error_string)."""
	url = PYPI_URL_TEMPLATE % (project, version)
	req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "win-llmtop-release-gate/1.0"})
	try:
		with urllib.request.urlopen(req, timeout=timeout) as resp:
			return resp.status, None
	except urllib.error.HTTPError as e:
		return e.code, None
	except Exception as e:
		return None, "%s: %s" % (type(e).__name__, e)


def main():
	try:
		version = read_pyproject_version()
		changelog_text = CHANGELOG_PATH.read_text(encoding="utf-8")
		citation_text = CITATION_PATH.read_text(encoding="utf-8")
		tags = get_fetched_tags()
	except Exception as e:
		print("release-gate: input error: %s" % e)
		return 1

	if is_released(version, tags):
		print("release-gate: not a release (tag v%s already exists); nothing to check" % version)
		return 0

	pypi_status, pypi_error = fetch_pypi_status(PYPI_PROJECT, version)
	ok, first_line, failures = evaluate(version, changelog_text, citation_text, tags, pypi_status, pypi_error)
	print(first_line)
	for err in failures:
		print(err)
	if ok:
		set_output("release", "true")
	return 0 if ok else 1


if __name__ == "__main__":
	sys.exit(main())
