#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run e2e_server.py's page checks against a rendered demo frame, so a layout change that breaks the e2e checks fails run_all.py first.

	Standard library only. e2e_server.py and docs/make_screenshot.py are loaded by path with importlib.util (neither is a package);
	importing them does not start a server or run their main(). The demo state is rendered to the Live page with colours off, and the
	resulting text is passed through _check_case_once_all / _check_case_once_live wrapped in a stand-in for subprocess.CompletedProcess.

	python -I tools/win-llmtop/tests/test_e2e_contract.py
"""

import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from llmtop_lib import pages, ui  # noqa: E402


def load_module(name, path):
	spec = importlib.util.spec_from_file_location(name, path)
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


e2e = load_module("e2e_server", os.path.join(HERE, "e2e_server.py"))
shot = load_module("make_screenshot", os.path.join(ROOT, "docs", "make_screenshot.py"))


def render_live(c, snap, width=120):
	"""The same frame render_frame_lines produces, but with colour forced off."""
	ui.C.enable(False)
	ui.STYLE["mode"] = "blocks"
	view = pages.View(3)  # 1h time window
	body = pages.live(c, snap, view, width, 55)
	lines = [pages.header(c, snap, view, width)]
	lines += pages.banner(c, snap, 4, width)
	lines += body
	lines.append(pages.footer(view, width))
	return [ui.clip(ln, width - 1) for ln in lines]


def proc_for(text):
	return types.SimpleNamespace(stdout=text, stderr="", returncode=0)


def without_turn_table(lines):
	"""Drop the recent-turns / recent-requests section (header through the next section rule)."""
	out, skipping = [], False
	for ln in lines:
		if "recent turns" in ln or "recent requests" in ln:
			skipping = True
			continue
		if skipping:
			if ln.startswith("── "):
				skipping = False
			else:
				continue
		out.append(ln)
	return out


def build():
	c, snap = shot.make_demo_state()
	# CI runs the e2e server in single-model mode; mirror that so the frame matches what CI renders
	snap = {**snap, "mode": "single", "notices": [*snap.get("notices", []), "single-model mode"]}
	model_name = next(iter(snap["presets"]))
	lines = render_live(c, snap, 120)
	return model_name, "\n".join(lines), lines


MODEL_NAME, TEXT, LINES = build()


def test_once_all_rows_parse():
	ok, detail = e2e._check_case_once_all(proc_for(TEXT))
	assert ok, "expected _check_case_once_all to pass, got (%r, %r)" % (ok, detail)


def test_once_live_model():
	ok, detail = e2e._check_case_once_live(proc_for(TEXT), MODEL_NAME)
	assert ok, "expected _check_case_once_live to pass for %r, got (%r, %r)" % (MODEL_NAME, ok, detail)


def test_regex_fails_on_empty():
	ok, detail = e2e._check_case_once_all(proc_for("\n".join(without_turn_table(LINES))))
	assert not ok, "expected _check_case_once_all to fail on the frame with the turn table removed, got (%r, %r)" % (ok, detail)


def test_colours_off():
	assert "\x1b[" not in TEXT, "rendered frame with colour off still contains an ANSI escape"


CASES = [
	("once_all_rows_parse", test_once_all_rows_parse),
	("once_live_model", test_once_live_model),
	("regex_fails_on_empty", test_regex_fails_on_empty),
	("colours_off", test_colours_off),
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
