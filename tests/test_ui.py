#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for llmtop_lib/ui.py: safe terminal output, SGR zero-width rules, and clipping.

	Standard library only. Tests safe(), vlen(), clip(), pad(), and rendered frames.

	python -I tools/win-llmtop/tests/test_ui.py
"""

import os
import re
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from llmtop_lib import pages, state as S, ui  # noqa: E402


class Ring:
	def cols(self, *args):
		return []

	def span_max(self, a, b):
		return float("nan")


class Rings:
	def __getitem__(self, key):
		return Ring()


def collector(requests=None, cfg=None):
	return types.SimpleNamespace(
		requests=requests or [], follow={}, flags={}, events=[],
		cfg=cfg or dict(S.CFG), rings=Rings(), allow=0.0, allow_known=False,
		alerts=types.SimpleNamespace(active={}, ordered=lambda: []),
		mem={}, calib={}, calib_key=lambda k: k, ensure_map=lambda name: None,
		pids=[], models=[], cpu_info={}
	)


def make_test_snap():
	g = {"name": "Test GPU", "vram": 24000.0, "budget": 20000.0, "ded": 17000.0, "shr": 2000.0, "uma": False}
	ll = {
		"present": True, "pids": [2000], "ded": 16000.0, "shr": 1735.0, "com": 16000.0, "ws": 1024.0, "pws": 512.0, "commit": 1100.0,
		"age": 1000.0, "floor": 525.0, "floor_known": True, "allow": 525.0, "allow_known": True, "allow_src": "measured",
		"over": 1210.0, "ws_drop": 0.0, "commit_drop": 0.0, "vm": None, "vm_error": None,
		"eng": {"3D": 50.0}, "cpu": 1.0,
	}
	return {
		"t": 1000.0, "router_up": True, "router_error": "", "router_auth_error": None, "presets": {},
		"gpu": g, "llama": ll,
		"ram": {"total": 65536.0, "avail": 32768.0, "commit": 20000.0, "commit_limit": 81920.0, "commit_max": 98304.0, "pagefiles": [], "standby": 1000.0, "pages_in": 0.0, "pages_out": 0.0, "free": 1000.0, "modified": 1000.0, "ws_total": 1000.0, "ws_by": {}, "pf_cfg": []},
		"cpu": {"pct": 10.0, "cores": 8, "threads": 16, "freq": 3500.0, "top": [], "cpu_cores": [10.0] * 8},
		"others": [], "busy": False, "new_events": [], "notices": [],
	}


def test_safe_table():
	table = [
		("plain_ascii", "Hello, World!", "Hello, World!"),
		("plain_non_ascii", "café München 🤖 简体中文", "café München 🤖 简体中文"),
		("tab", "col1\tcol2\t\tcol3", "col1 col2  col3"),
		("c0_controls", "a\x00b\x01c\x07d\x0ce\x1ff", "abcdef"),
		("del", "a\x7fb", "ab"),
		("c1_controls", "a\x80b\x85c\x90d\x9fe", "abcde"),
		("bidi_lrm_rlm", "a\u200eb\u200fc", "abc"),
		("bidi_embeddings", "a\u202ab\u202bc\u202cd\u202de\u202ef", "abcdef"),
		("bidi_isolates", "a\u2066b\u2067c\u2068d\u2069e", "abcde"),
		("csi_sgr", "a\x1b[31;1mb\x1b[0mc", "abc"),
		("csi_non_sgr", "a\x1b[2Jb\x1b[?25hc\x1b[Hd", "abcd"),
		("osc_bel", "a\x1b]0;my title\x07b", "ab"),
		("osc_st", "a\x1b]0;my title\x1b\\b", "ab"),
		("osc_unterminated", "a\x1b]0;my title", "a"),
		("dcs", "a\x1bPmy data\x1b\\b", "ab"),
		("sos", "a\x1bXmy data\x1b\\b", "ab"),
		("pm", "a\x1b^my data\x1b\\b", "ab"),
		("apc", "a\x1b_my data\x1b\\b", "ab"),
		("2_char_esc", "a\x1bMb\x1b7c\x1b8d\x1b=e", "abcde"),
		("trailing_esc", "a\x1b", "a"),
	]
	for label, inp, expected in table:
		res = ui.safe(inp)
		assert res == expected, "%s: expected %r, got %r" % (label, expected, res)


def test_vlen_clip_pad():
	# String with SGR plus a non-SGR CSI and an OSC
	s = "start\x1b[31m red \x1b[2J\x1b]0;title\x07end\x1b[0m"
	# Visible characters: "start red end" (length 13)
	assert ui.vlen(s) == 13, "expected vlen 13, got %d" % ui.vlen(s)

	# pad: adds 7 spaces to reach width 20
	padded = ui.pad(s, 20)
	assert ui.vlen(padded) == 20, "expected vlen(pad) 20, got %d" % ui.vlen(padded)
	assert padded.endswith("       "), "expected 7 padding spaces at end"

	# rpad: prepends 7 spaces to reach width 20
	rpadded = ui.rpad(s, 20)
	assert ui.vlen(rpadded) == 20, "expected vlen(rpad) 20, got %d" % ui.vlen(rpadded)
	assert rpadded.startswith("       "), "expected 7 padding spaces at start"

	# clip without truncation (width 20 >= 13):
	# non-SGR CSI and OSC removed, SGR intact
	c20 = ui.clip(s, 20)
	assert c20 == "start\x1b[31m red end\x1b[0m", "unexpected clip 20: %r" % c20
	assert "\x1b[2J" not in c20
	assert "\x1b]" not in c20
	# Assert no ESC except SGR
	non_sgr = re.findall(r"\x1b(?!\[[0-9;]*m)", c20)
	assert not non_sgr, "c20 has non-SGR ESC: %r" % non_sgr

	# clip with truncation (width 8 < 13):
	# 7 visible chars + "…" + C.reset
	c8 = ui.clip(s, 8)
	assert ui.vlen(c8) == 8, "expected vlen(clip 8) == 8, got %d" % ui.vlen(c8)
	assert c8 == "start\x1b[31m r…\x1b[0m", "unexpected clip 8: %r" % c8
	non_sgr_c8 = re.findall(r"\x1b(?!\[[0-9;]*m)", c8)
	assert not non_sgr_c8, "c8 has non-SGR ESC: %r" % non_sgr_c8


def test_rendered_frame_safe():
	bad_seq = "\x1b[2J\x1b]0;title\x07\x1bPdata\x1b\\\x1bM\x00\x7f\x82\u200e\u202a"
	model_id = "test-model" + bad_seq
	c = collector()
	c.flags = {model_id: ({}, "")}
	m = {
		"name": model_id, "arch": "llama", "n_layer": 1,
		"layers": {0: {"kind": "full", "mib": 100.0, "gpu": 100.0}}, "verified": True,
		"ctx": 4096, "cache_k": "q8_0", "cache_v": "q8_0",
		"checks": [], "assumed": [], "groups": {},
		"kv": {"layers": {"full": 0, "swa": 0}, "gpu": 0, "mtp": 0, "full": 0, "swa": 0, "per_token": 0},
		"rec": {"total": 0, "layers": 0, "per_layer": 0},
		"mtp_mib": 0, "draft": None, "fit": None,
		"weights": {"cpu": 0.0}, "roster": [], "ckpt_max": 0, "cache_ram": 0,
		"n_expert": 0, "n_expert_used": 0,
	}
	c.mem = {model_id: {"state": "ready", "map": m, "key": "k"}}
	ev = {
		"t": 100.0, "wall": 1000.0, "level": "warn", "kind": "log",
		"msg": "alert " + bad_seq, "detail": "detail " + bad_seq,
		"preset": model_id, "session": 0
	}
	c.events = [ev]

	snap = make_test_snap()
	snap["router_error"] = "server error " + bad_seq
	snap["router_up"] = False
	snap["presets"] = {model_id: {"status": "loaded", "slot": None, "meta": {}, "slot_error": "slot err " + bad_seq}}

	re_non_sgr = re.compile(r"\x1b(?!\[[0-9;]*m)")

	for w in (80, 120):
		for page_idx in range(6):
			view = pages.View()
			view.page = page_idx
			view.msg = "collector error " + bad_seq
			rendered = pages.render(c, snap, view, w, 40)
			for i, ln in enumerate(rendered):
				bad_esc = re_non_sgr.findall(ln)
				assert not bad_esc, "Page %d line %d width %d has non-SGR escape: %r in %r" % (page_idx, i, w, bad_esc, ln)
				vl = ui.vlen(ln)
				assert vl <= w, "Page %d line %d width %d has visible length %d > %d: %r" % (page_idx, i, w, vl, w, ln)


def test_fast_and_slow_paths_identical():
	table_inputs = [
		"Hello, World!",
		"café München 🤖 简体中文",
		"col1\tcol2\t\tcol3",
		"a\x00b\x01c\x07d\x0ce\x1ff",
		"a\x7fb",
		"a\x80b\x85c\x90d\x9fe",
		"a\u200eb\u200fc",
		"a\u202ab\u202bc\u202cd\u202de\u202ef",
		"a\u2066b\u2067c\u2068d\u2069e",
		"a\x1b[31;1mb\x1b[0mc",
		"a\x1b[2Jb\x1b[?25hc\x1b[Hd",
		"a\x1b]0;my title\x07b",
		"a\x1b]0;my title\x1b\\b",
		"a\x1b]0;my title",
		"a\x1bPmy data\x1b\\b",
		"a\x1bXmy data\x1b\\b",
		"a\x1b^my data\x1b\\b",
		"a\x1b_my data\x1b\\b",
		"a\x1bMb\x1b7c\x1b8d\x1b=e",
		"a\x1b",
	]
	sgr_inputs = [
		"\x1b[31mred\x1b[0m",
		"\x1b[1m\x1b[32mbold green\x1b[0m text",
		"%slabel%s: %svalue%s (%salert%s)" % (ui.C.grey, ui.C.reset, ui.C.bcyan, ui.C.reset, ui.C.bred, ui.C.reset),
		"a" * 100,
		"%s%s%s" % (ui.C.bold, "x" * 80, ui.C.reset),
	]
	all_inputs = table_inputs + sgr_inputs

	def reference_slow_clip(s, width):
		if len(ui.safe(s)) <= width:
			out = []
			for m in ui.RE_TOKEN.finditer(s):
				if m.group(1):
					out.append(m.group(1))
				elif m.group(2) or m.group(4):
					continue
				elif m.group(3):
					out.append(" ")
				else:
					out.append(m.group(5))
			return "".join(out)
		out, n = [], 0
		for m in ui.RE_TOKEN.finditer(s):
			if m.group(1):
				out.append(m.group(1))
			elif m.group(2) or m.group(4):
				continue
			elif n < width - 1:
				out.append(" " if m.group(3) else m.group(5))
				n += 1
			else:
				break
		return "".join(out) + "…" + ui.C.reset

	def reference_slow_vlen(s):
		return len(ui.safe(s))

	for inp in all_inputs:
		fast_v = ui.vlen(inp)
		slow_v = reference_slow_vlen(inp)
		assert fast_v == slow_v, "vlen mismatch on %r: %d != %d" % (inp, fast_v, slow_v)

		for w in (0, 1, 5, 10, 20, 50, 150):
			actual = ui.clip(inp, w)
			expected = reference_slow_clip(inp, w)
			assert actual == expected, "clip mismatch on %r (width %d):\n  got:      %r\n  expected: %r" % (inp, w, actual, expected)


CASES = [
	("safe_table", test_safe_table),
	("vlen_clip_pad", test_vlen_clip_pad),
	("rendered_frame_safe", test_rendered_frame_safe),
	("fast_and_slow_paths_identical", test_fast_and_slow_paths_identical),
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
