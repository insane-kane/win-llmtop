#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for llmtop_lib/pages.py: speed figures for short requests, GPUs that report no VRAM, and wording that must not assume a router.

	Standard library only. The functions are tested directly with hand-built inputs: a SimpleNamespace collector
	(requests, follow, cfg, rings) and hand-built snap dicts, so no server and no Windows counters are needed.

	python -I tools/win-llmtop/tests/test_pages.py
"""

import math
import os
import re
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from llmtop_lib import pages, state as S  # noqa: E402


def req(preset="m", pn=120, pp=100.0, tn=30, tg=5.0, wall=1000.0):
	return {"preset": preset, "pn": pn, "pp": pp, "tn": tn, "tg": tg, "wall": wall, "wall0": wall - 1.0, "task": "chat", "ms": 1000.0}


class Ring:
	def cols(self, *args):
		return []

	def span_max(self, a, b):
		return float("nan")


class Rings:
	def __getitem__(self, key):
		return Ring()


def collector(requests, cfg=None):
	return types.SimpleNamespace(requests=requests, follow={}, flags={}, events=[], cfg=cfg or {}, rings=Rings(), allow=0.0, allow_known=False)


def gpu_snap(vram, budget, ded=1000.0):
	return {"t": 1000.0, "gpu": {"name": "Test GPU", "vram": vram, "budget": budget, "ded": ded}, "llama": {"ded": ded, "present": False}, "others": []}


def test_summary_short_requests_fall_back():
	c = collector([req(), req(), req()])
	s = pages.preset_summary(c, "m")
	assert s["pp_min"] == 32 and s["tg_min"] == 20, s
	assert s["pps"] and s["tgs"]
	assert not math.isnan(S.median(s["pps"]))
	assert not math.isnan(S.median(s["tgs"]))


def test_summary_long_cutoffs_when_reached():
	c = collector([req(), req(), req(pn=2000, pp=500.0, tn=100, tg=30.0)])
	s = pages.preset_summary(c, "m")
	assert s["pp_min"] == 1000 and s["tg_min"] == 50, s
	assert s["pps"] == [500.0]
	assert s["tgs"] == [30.0]


def test_gpu_block_no_vram_with_budget():
	out = pages.gpu_block(collector([]), gpu_snap(0, 8192), pages.View(), 120, 0)
	assert any("OS budget" in ln for ln in out), out


def test_gpu_block_no_vram_no_budget():
	out = pages.gpu_block(collector([]), gpu_snap(0, 0), pages.View(), 120, 0)
	assert any("no dedicated memory" in ln for ln in out), out


def test_header_single_mode_says_server():
	for up, word in ((True, "up"), (False, "DOWN")):
		line = pages.header(None, {"t": 1000.0, "router_up": up, "mode": "single"}, pages.View(), 120)
		assert ("● server %s" % word) in line, line


def test_header_without_mode_says_router():
	line = pages.header(None, {"t": 1000.0, "router_up": True}, pages.View(), 120)
	assert "● router up" in line, line


CFG = {"log_dir": None, "spill_mib": 256, "rival_gpu_pct": 20}


def plain_requests(c):
	snap = {"t": 2000.0}
	return [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in pages.requests(c, snap, pages.View(), 200, 60)]


def test_requests_subtitle_per_preset_cutoffs():
	c = collector([req(preset="m"), req(preset="n", pn=2000, pp=500.0, tn=100, tg=30.0)], cfg=dict(CFG))
	text = "\n".join(plain_requests(c))
	assert "per preset" in text, text
	assert "m 32+/20+" in text, text
	assert "n 1,000+/50+" in text, text
	assert "32+ new tokens" in text and "20+ tokens" in text, text


def test_requests_subtitle_single_cutoff():
	c = collector([req(preset="m", pn=2000, pp=500.0, tn=100, tg=30.0)], cfg=dict(CFG))
	text = "\n".join(plain_requests(c))
	assert "per preset" not in text, text
	assert "1,000+ new tokens" in text and "50+ tokens" in text, text


def test_requests_empty_message():
	assert "no finished requests yet" in "\n".join(plain_requests(collector([], cfg=dict(CFG))))
	cfg = dict(CFG)
	cfg["log_dir"] = "logs"
	assert "no finished requests in logs yet" in "\n".join(plain_requests(collector([], cfg=cfg)))


def test_speed_table_no_labels_when_cutoffs_match():
	# two presets that both reach the long cutoffs (a preset with no requests cannot appear in the table: presets come from c.requests)
	c = collector([req(preset="alpha", pn=2000, pp=500.0, tn=100, tg=30.0), req(preset="beta", pn=3000, pp=600.0, tn=200, tg=40.0)], cfg=dict(CFG))
	lines = plain_requests(c)
	sub = next(ln for ln in lines if "speed, t/s" in ln)
	assert "1,000+ new tokens" in sub and "50+ tokens" in sub and "per preset" not in sub, sub
	assert "1,000+/50+" not in "\n".join(lines), lines


def test_speed_table_labels_stay_in_column():
	c = collector([req(preset="long-model-name", pn=2000, pp=500.0, tn=100, tg=30.0), req(preset="short", pn=120, pp=100.0, tn=30, tg=5.0)], cfg=dict(CFG))
	lines = plain_requests(c)
	header = next(ln for ln in lines if "prefill median" in ln)
	h = header.index("prefill median")
	rows = lines[lines.index(header) + 1:lines.index(header) + 3]
	assert any("1,000+/50+" in ln for ln in rows), rows
	assert any("32+/20+" in ln for ln in rows), rows
	assert any("..." in ln[2:26] for ln in rows), rows  # the long name is shortened from the start, keeping its end
	for ln in rows:  # the first number is right-aligned to the same right edge as "prefill median"
		m = re.search(r"\d", ln[26:])
		assert m, ln
		num = re.match(r"\d+", ln[26 + m.start():]).group(0)
		assert 26 + m.start() + len(num) == h + len("prefill median"), ln


def plain(lines):
	return [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines]


def live_snap(notices):
	return {"t": 1000.0, "notices": notices, "presets": {}, "gpu": None, "llama": {"present": False}, "ram": None}


def test_notices_shown_on_live_page():
	snap = live_snap(["single-model mode", "server unreachable (tried a, b)"])
	text = "\n".join(plain(pages.live(collector([]), snap, pages.View(), 120, 40)))
	assert "· single-model mode" in text, text
	assert "· server unreachable (tried a, b)" in text, text


def test_notices_capped_with_more_line():
	snap = live_snap(["notice %d" % i for i in range(6)])
	text = "\n".join(plain(pages.live(collector([]), snap, pages.View(), 120, 40)))
	assert "· notice 0" in text and "· notice 2" in text, text
	assert "notice 3" not in text, text
	assert "+3 more (see the Events page)" in text, text


def test_no_notice_lines_when_list_empty():
	out = plain(pages.live(collector([]), live_snap([]), pages.View(), 120, 40))
	assert not any(ln.startswith("  ·") for ln in out), out


def test_events_page_lists_all_notices():
	c = collector([])
	snap = {"t": 1000.0, "notices": ["notice %d" % i for i in range(6)]}
	text = "\n".join(plain(pages.events(c, snap, pages.View(), 120, 40)))
	for i in range(6):
		assert "· notice %d" % i in text, text
	assert "more (see the Events page)" not in text, text


def test_slot_error_replaces_waiting_text():
	c = collector([])
	snap = {"t": 1000.0, "llama": {"ded": 0.0}}
	base = {"status": "loaded", "slot": None, "meta": {}}
	text = "\n".join(plain(pages.model_block(c, snap, pages.View(), "m", dict(base, slot_error="HTTP 404"), 120, 2)))
	assert "slot data unavailable: HTTP 404" in text, text
	assert "waiting for slot data" not in text, text
	text = "\n".join(plain(pages.model_block(c, snap, pages.View(), "m", base, 120, 2)))
	assert "waiting for slot data…" in text, text


def test_pm_error_shows_unavailable_not_zero():
	rm = {"total": 32768.0, "avail": 8192.0, "commit": 4096.0, "commit_max": 24576.0, "pagefiles": [], "standby": 2048.0, "free": 4096.0, "pages_in": 0.0, "pages_out": 0.0}
	ll = {"present": True, "ws": 0.0, "commit": 0.0, "cpu": 0.0, "pm_error": "access denied"}
	cfg = dict(CFG, pagein_warn=100.0)
	text = "\n".join(plain(pages.ram_block(collector([], cfg=cfg), {"t": 1000.0, "ram": rm, "llama": ll}, pages.View(), 120, 2)))
	assert "unavailable (access denied)" in text, text
	assert "0.0 GiB" not in text, text


def test_vm_error_replaces_walk_text():
	ll = {"present": True, "ws": 1024.0, "pws": 512.0, "commit": 2048.0, "cpu": 1.5, "ded": 0.0, "shr": 0.0, "vm": None, "vm_error": "access denied"}
	m = {"fit": None, "weights": {"cpu": 0.0}, "roster": [], "ckpt_max": 0, "cache_ram": 0}
	text = "\n".join(plain(pages.host_lines(collector([]), {"t": 1000.0, "llama": ll}, "m", m, {"m"})))
	assert "unavailable (access denied)" in text, text
	assert "address-space walk running" not in text, text
	ll = dict(ll, vm_error=None)
	text = "\n".join(plain(pages.host_lines(collector([]), {"t": 1000.0, "llama": ll}, "m", m, {"m"})))
	assert "address-space walk running…" in text, text


def test_uma_vram_bar_scales_against_budget():
	pal = [pages.C.bcyan, pages.C.bgreen]
	line = plain([pages.vram_bar({"name": "iGPU", "vram": 0, "budget": 8192.0, "uma": True}, [("weights", 4096.0), ("KV", 1024.0)], 40, pal)])[0]
	assert "shared GPU memory (no dedicated VRAM)" in line, line
	assert "8.0 GiB" in line, line
	line = plain([pages.vram_bar({"vram": 8192.0, "budget": 16384.0, "uma": False}, [("weights", 4096.0)], 40, pal)])[0]
	assert "OS budget" in line, line


def test_header_mode_none_server_down_says_not_found():
	line = pages.header(None, {"t": 1000.0, "router_up": False, "mode": None}, pages.View(), 120)
	assert "● server not found" in line, line


CASES = [
	("summary_short_requests_fall_back", test_summary_short_requests_fall_back),
	("summary_long_cutoffs_when_reached", test_summary_long_cutoffs_when_reached),
	("gpu_block_no_vram_with_budget", test_gpu_block_no_vram_with_budget),
	("gpu_block_no_vram_no_budget", test_gpu_block_no_vram_no_budget),
	("header_single_mode_says_server", test_header_single_mode_says_server),
	("header_without_mode_says_router", test_header_without_mode_says_router),
	("requests_subtitle_per_preset_cutoffs", test_requests_subtitle_per_preset_cutoffs),
	("requests_subtitle_single_cutoff", test_requests_subtitle_single_cutoff),
	("requests_empty_message", test_requests_empty_message),
	("speed_table_no_labels_when_cutoffs_match", test_speed_table_no_labels_when_cutoffs_match),
	("speed_table_labels_stay_in_column", test_speed_table_labels_stay_in_column),
	("notices_shown_on_live_page", test_notices_shown_on_live_page),
	("notices_capped_with_more_line", test_notices_capped_with_more_line),
	("no_notice_lines_when_list_empty", test_no_notice_lines_when_list_empty),
	("events_page_lists_all_notices", test_events_page_lists_all_notices),
	("slot_error_replaces_waiting_text", test_slot_error_replaces_waiting_text),
	("pm_error_shows_unavailable_not_zero", test_pm_error_shows_unavailable_not_zero),
	("vm_error_replaces_walk_text", test_vm_error_replaces_walk_text),
	("uma_vram_bar_scales_against_budget", test_uma_vram_bar_scales_against_budget),
	("header_mode_none_server_down_says_not_found", test_header_mode_none_server_down_says_not_found),
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
