#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for .github/scripts/release_gate.py (no network).

	python -I tools/win-llmtop/tests/test_release_gate.py
"""

import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / ".github" / "scripts" / "release_gate.py"
RELEASE_YML_PATH = ROOT / ".github" / "workflows" / "release.yml"

_spec = importlib.util.spec_from_file_location("release_gate", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
release_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_gate)

VALID_VERSION = "0.1.1"
VALID_CHANGELOG = (
	"# Changelog\n\n"
	"## [Unreleased]\n\n"
	"## [0.1.1] - 2026-10-20\n\n"
	"### Added\n\n"
	"- Something new.\n\n"
	"## [0.1.0] - 2026-10-20\n"
)
VALID_CITATION = (
	"cff-version: 1.2.0\n"
	'title: "win-llmtop"\n'
	'version: "0.1.1"\n'
	'date-released: "2026-10-20"\n'
)
VALID_TAGS = ["v0.1.0"]


def test_all_pass_together():
	ok, first_line, failures = release_gate.evaluate(
		VALID_VERSION,
		VALID_CHANGELOG,
		VALID_CITATION,
		VALID_TAGS,
		404,
	)
	assert ok is True, (ok, first_line, failures)
	assert failures == [], failures
	assert first_line == "release-gate: 0.1.1 is a new release", first_line


def test_condition_version_format_fails_alone():
	bad_ver = "0.1"
	changelog = "## [0.1] - 2026-10-20\n"
	citation = 'version: "0.1"\n'
	ok, first_line, failures = release_gate.evaluate(bad_ver, changelog, citation, VALID_TAGS, 404)
	assert ok is False
	assert len(failures) == 1, failures
	assert "pyproject.toml" in failures[0] and "X.Y.Z" in failures[0], failures
	assert first_line.startswith("release-gate: not a release ("), first_line
	assert "expected on non-release commits" in first_line, first_line


def test_condition_changelog_fails_alone():
	# Missing heading for 0.1.1
	no_heading = "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-10-20\n"
	ok, first_line, failures = release_gate.evaluate(VALID_VERSION, no_heading, VALID_CITATION, VALID_TAGS, 404)
	assert ok is False
	assert len(failures) == 1, failures
	assert "CHANGELOG.md" in failures[0], failures
	assert first_line.startswith("release-gate: not a release ("), first_line

	# Heading present but invalid calendar date (Feb 30)
	bad_date = "# Changelog\n\n## [0.1.1] - 2026-02-30\n"
	ok2, _line2, failures2 = release_gate.evaluate(VALID_VERSION, bad_date, VALID_CITATION, VALID_TAGS, 404)
	assert ok2 is False
	assert len(failures2) == 1, failures2
	assert "invalid date" in failures2[0], failures2


def test_condition_citation_fails_alone():
	stale_citation = 'cff-version: 1.2.0\nversion: "0.1.0"\n'
	ok, first_line, failures = release_gate.evaluate(VALID_VERSION, VALID_CHANGELOG, stale_citation, VALID_TAGS, 404)
	assert ok is False
	assert len(failures) == 1, failures
	assert "CITATION.cff" in failures[0], failures
	assert first_line.startswith("release-gate: not a release ("), first_line

	missing_citation = "cff-version: 1.2.0\ntitle: win-llmtop\n"
	ok2, _line2, failures2 = release_gate.evaluate(VALID_VERSION, VALID_CHANGELOG, missing_citation, VALID_TAGS, 404)
	assert ok2 is False
	assert len(failures2) == 1, failures2
	assert "CITATION.cff" in failures2[0], failures2


def test_condition_tag_absent_fails_alone():
	err = release_gate.check_tag_absent("0.1.1", ["v0.1.0", "v0.1.1"])
	assert err is not None and "v0.1.1" in err, err
	assert release_gate.check_tag_absent("0.1.1", ["v0.1.0"]) is None


def test_condition_version_greater_fails_alone():
	# v0.1.1 is not in tags (so check_tag_absent passes), but v0.2.0 exists (so check_version_greater fails alone)
	ok, first_line, failures = release_gate.evaluate(
		VALID_VERSION,
		VALID_CHANGELOG,
		VALID_CITATION,
		["v0.1.0", "v0.2.0"],
		404,
	)
	assert ok is False
	assert len(failures) == 1, failures
	assert "not greater than existing tag v0.2.0" in failures[0], failures
	assert first_line.startswith("release-gate: not a release ("), first_line


def test_condition_pypi_fails_alone():
	# HTTP 200 means version is already on PyPI
	ok, first_line, failures = release_gate.evaluate(VALID_VERSION, VALID_CHANGELOG, VALID_CITATION, VALID_TAGS, 200)
	assert ok is False
	assert len(failures) == 1, failures
	assert "HTTP 200" in failures[0], failures
	assert first_line.startswith("release-gate: not a release ("), first_line

	# Unexpected HTTP status fails closed
	ok2, _line2, failures2 = release_gate.evaluate(VALID_VERSION, VALID_CHANGELOG, VALID_CITATION, VALID_TAGS, 503)
	assert ok2 is False
	assert len(failures2) == 1, failures2
	assert "503" in failures2[0], failures2

	# Network/request error fails closed with the error printed
	ok3, _line3, failures3 = release_gate.evaluate(
		VALID_VERSION,
		VALID_CHANGELOG,
		VALID_CITATION,
		VALID_TAGS,
		None,
		"URLError: timed out",
	)
	assert ok3 is False
	assert len(failures3) == 1, failures3
	assert "URLError: timed out" in failures3[0], failures3


def test_prerelease_version_rejected():
	pre = "0.1.1rc1"
	changelog = "## [0.1.1rc1] - 2026-10-20\n"
	citation = 'version: "0.1.1rc1"\n'
	assert release_gate.check_version_format(pre) is not None
	ok, first_line, failures = release_gate.evaluate(pre, changelog, citation, VALID_TAGS, 404)
	assert ok is False
	assert len(failures) == 1, failures
	assert "0.1.1rc1" in failures[0], failures
	assert first_line.startswith("release-gate: not a release ("), first_line


def test_equal_and_lower_versions_rejected():
	tags = ["v0.1.0", "v0.1.2"]
	# Equal to newest tag (0.1.2)
	err_eq = release_gate.check_version_greater("0.1.2", tags)
	assert err_eq is not None and "v0.1.2" in err_eq, err_eq
	# Lower than newest tag (0.1.1 < 0.1.2, and 0.0.9 < 0.1.2)
	err_low1 = release_gate.check_version_greater("0.1.1", tags)
	assert err_low1 is not None and "v0.1.2" in err_low1, err_low1
	err_low2 = release_gate.check_version_greater("0.0.9", tags)
	assert err_low2 is not None and "v0.1.2" in err_low2, err_low2
	# Integer tuple comparison (0.1.10 > 0.1.2, even though "10" < "2" lexicographically)
	assert release_gate.check_version_greater("0.1.10", tags) is None


def test_changelog_regex_agrees_with_release_yml():
	release_yml = RELEASE_YML_PATH.read_text(encoding="utf-8")
	m = re.search(r"re\.search\((.+?),\s*text,\s*re\.M\)", release_yml)
	assert m is not None, "could not find changelog re.search(...) in .github/workflows/release.yml"
	yml_expr = m.group(1).strip()

	script_src = SCRIPT_PATH.read_text(encoding="utf-8")
	assert yml_expr in script_src, (
		"release_gate.py does not contain the exact changelog regex expression from release.yml: %s" % yml_expr
	)

	for sample in ("0.1.0", "0.1.1", "0.2.0", "1.10.2"):
		expected = eval(yml_expr, {"re": re, "version": sample})
		actual = release_gate.changelog_pattern(sample)
		assert actual == expected, (sample, actual, expected)


CASES = [
	("all_pass_together", test_all_pass_together),
	("condition_version_format_fails_alone", test_condition_version_format_fails_alone),
	("condition_changelog_fails_alone", test_condition_changelog_fails_alone),
	("condition_citation_fails_alone", test_condition_citation_fails_alone),
	("condition_tag_absent_fails_alone", test_condition_tag_absent_fails_alone),
	("condition_version_greater_fails_alone", test_condition_version_greater_fails_alone),
	("condition_pypi_fails_alone", test_condition_pypi_fails_alone),
	("prerelease_version_rejected", test_prerelease_version_rejected),
	("equal_and_lower_versions_rejected", test_equal_and_lower_versions_rejected),
	("changelog_regex_agrees_with_release_yml", test_changelog_regex_agrees_with_release_yml),
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
