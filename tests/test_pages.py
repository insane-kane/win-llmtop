#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Aaron Kane <software@kanenet.us>
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
from llmtop_lib import pages, state as S, ui  # noqa: E402


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
	return types.SimpleNamespace(requests=requests, follow={}, flags={}, events=[], cfg=cfg or {}, rings=Rings(), allow=0.0, allow_known=False, alerts=types.SimpleNamespace(active={}), mem={}, calib={}, calib_key=lambda k: k, ensure_map=lambda name: None)


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
	assert "32+ new tokens" in text and "decode over 20+" in text, text


def test_requests_subtitle_single_cutoff():
	c = collector([req(preset="m", pn=2000, pp=500.0, tn=100, tg=30.0)], cfg=dict(CFG))
	text = "\n".join(plain_requests(c))
	assert "per preset" not in text, text
	assert "1,000+ new tokens" in text and "decode over 50+" in text, text


def test_requests_empty_message():
	assert "no finished requests yet" in "\n".join(plain_requests(collector([], cfg=dict(S.CFG))))
	cfg = dict(CFG)
	cfg["log_dir"] = "logs"
	assert "no finished requests in logs yet" in "\n".join(plain_requests(collector([], cfg=cfg)))


def test_speed_table_no_labels_when_cutoffs_match():
	# two presets that both reach the long cutoffs (a preset with no requests cannot appear in the table: presets come from c.requests)
	c = collector([req(preset="alpha", pn=2000, pp=500.0, tn=100, tg=30.0), req(preset="beta", pn=3000, pp=600.0, tn=200, tg=40.0)], cfg=dict(CFG))
	lines = plain_requests(c)
	sub = next(ln for ln in lines if "speed, t/s" in ln)
	assert "1,000+ new tokens" in sub and "decode over 50+" in sub and "per preset" not in sub, sub
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
	text = "\n".join(plain(pages.ram_block(collector([]), {"t": 1000.0, "ram": rm, "llama": ll}, pages.View(), 120, 2)))
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


def test_requests_v2_colours_notes_and_nested_turns():
	assert pages.rel(float("nan"), 100.0) == pages.C.grey
	assert pages.rel(100.0, float("nan")) == pages.C.grey
	assert pages.rel(100.0, 0.0) == pages.C.grey
	assert pages.rel(125.0, 100.0) == pages.C.bcyan  # a turn far faster than usual is blue, not green
	assert pages.rel(100.0, 100.0) == pages.C.bgreen
	assert pages.rel(80.0, 100.0) == pages.C.bgreen
	assert pages.rel(60.0, 100.0) == pages.C.byellow
	assert pages.rel(40.0, 100.0) == pages.C.bred

	r1 = dict(req(preset="alpha", pn=500, pp=400.0, tn=40, tg=50.0, wall=1000.0), slot=0, cached=2000, kv=2540, sel="LRU", evicted=12000, trunc=True, reallocs=1, spill=1500.0)
	r2 = dict(req(preset="alpha", pn=300, pp=420.0, tn=30, tg=52.0, wall=1005.0), slot=0, cached=2540, kv=2870, sel="LCP", sim=0.9, keep=1.0)
	c = collector([r1, r2], cfg=dict(CFG))
	c.allow, c.allow_known = 271.2, True
	snap = {"t": 2000.0}
	view = pages.View()
	collapsed = plain(pages.requests(c, snap, view, 200, 80))
	text = "\n".join(collapsed)
	assert "1 prompts · 2 turns · all presets   (m: preset  t: turns)" in text, text
	assert "cache reset 12k" in text and "cut off" in text and "shared +1.2 GiB" in text and "resized" in text, text
	assert "LCP" not in text, text
	assert "slowest prefills" not in text, text
	assert "1 cache reset · 1 cut off" in text, text

	view.turns = True
	expanded = plain(pages.requests(c, snap, view, 200, 80))
	exp_text = "\n".join(expanded)
	assert any("  · " in ln for ln in expanded), exp_text
	assert any("  └ " in ln for ln in expanded), exp_text

	recent = plain(pages.recent_requests(c, snap, view, 140, 10))
	rec_text = "\n".join(recent)
	assert "recent turns" in rec_text, rec_text
	assert any("▸ " in ln for ln in recent), rec_text


def test_prompt_and_turn_rows_have_hit_and_draft():
	r1 = dict(req(preset="alpha", pn=500, pp=400.0, tn=40, tg=50.0, wall=1000.0), slot=0, cached=2000, kv=2540, acc=0.88, dacc=308, dgen=350)
	r2 = dict(req(preset="alpha", pn=300, pp=420.0, tn=30, tg=52.0, wall=1005.0), slot=0, cached=2540, kv=2870, acc=0.91, dacc=30, dgen=33)
	c = collector([r1, r2], cfg=dict(CFG))
	snap = {"t": 2000.0}
	view = pages.View()
	st = pages.bucket_stats(c, view, snap)
	ps = pages.prompt_groups(c, view, snap)[0]
	pw, drop, _notes = pages.prompt_layout(200, ["alpha"], True)
	pr = plain([pages.prompt_row(c, ps[0], st, True, pw=pw, drop=drop)])[0]
	tr = plain([pages.turn_row(c, r1, st, True, "  · ", True, pw=pw, drop=drop, nested=True)])[0]
	# prompt: hit = 4540 / 5340, draft = 338 / 383; turn: hit = 2000 / 2500, draft = 0.88
	assert "85%" in pr and "88%" in pr, pr
	assert "80%" in tr and "88%" in tr, tr
	assert "alpha" in pr and "alpha" not in tr, (pr, tr)  # the nested turn's preset cell is blank; the prompt row names it


def test_requests_fit_width_100():
	r1 = dict(req(preset="alpha", pn=500, pp=400.0, tn=40, tg=50.0, wall=1000.0), slot=0, cached=2000, kv=2540, acc=0.88)
	r2 = dict(req(preset="alpha", pn=300, pp=420.0, tn=30, tg=52.0, wall=1005.0), slot=0, cached=2540, kv=2870, acc=0.91)
	c = collector([r1, r2], cfg=dict(CFG))
	view = pages.View()
	view.turns = True
	lines = plain(pages.requests(c, {"t": 2000.0}, view, 100, 80))
	i = next(i for i, ln in enumerate(lines) if "every prompt" in ln)
	tab = lines[i + 2:]  # the legend, then the header and the rows
	header = tab[0]
	assert "prefill" in header and "decode" in header and "hit" in header and "draft" in header, header
	assert "cached" not in header and "gen" not in header, header  # the narrowest columns are dropped first
	for ln in tab:
		assert len(ln) <= 100, ln


def test_by_context_tables_ranked_colours():
	# three turns per preset in the same column: with two ranked presets the fastest is green and the slowest red
	rs = []
	for i in range(3):
		rs.append(dict(req(preset="alpha", pn=500, pp=150.0, tn=40, tg=30.0, wall=1000.0 + i), slot=0, cached=1000, kv=1540))
		rs.append(dict(req(preset="beta", pn=500, pp=200.0, tn=40, tg=40.0, wall=1000.0 + i), slot=1, cached=1000, kv=1540))
	c = collector(rs, cfg=dict(CFG))
	lines = pages.requests(c, {"t": 2000.0}, pages.View(), 200, 60)
	i = next(i for i, ln in enumerate(lines) if "prefill t/s by context" in ln)
	j = next(j for j, ln in enumerate(lines) if "decode t/s by context" in ln)
	k = next(k for k, ln in enumerate(lines) if "per turn" in ln)
	assert "green fastest · red slowest in each column" in lines[i] and "green fastest · red slowest in each column" in lines[j], (lines[i], lines[j])
	pre = "".join(lines[i + 2:j])  # the column header row, then one row per preset
	dec = "".join(lines[j + 2:k])
	assert pages.C.bgreen in pre and pages.C.bgreen in dec, (pre, dec)  # beta is the fastest of the two
	assert pages.C.bred in pre and pages.C.bred in dec, (pre, dec)  # alpha is the slowest of the two
	assert pages.C.bold not in pre and pages.C.bold not in dec, (pre, dec)


def test_effect_section_removed():
	c = collector([req(preset="alpha", pn=500, pp=400.0, tn=40, tg=50.0, wall=1000.0), req(preset="alpha", pn=300, pp=420.0, tn=30, tg=52.0, wall=1005.0)], cfg=dict(CFG))
	text = "\n".join(plain_requests(c))
	assert "effect of shared GPU memory" not in text, text
	assert not hasattr(pages, "rival_of") and not hasattr(pages, "effect_lines")
	assert hasattr(pages, "spill_of")  # the notes and the per-turn graph still use it


def test_rank_colours():
	nan = float("nan")
	# n = 1: the only ranked preset is plain (there is nothing to compare it to)
	assert pages.rank_colours([(100.0, 3)]) == [""]
	# n = 2: k = 1, fastest green, slowest red
	assert pages.rank_colours([(100.0, 3), (50.0, 3)]) == [pages.C.bgreen, pages.C.bred]
	# n = 3: top green, bottom red, middle yellow
	assert pages.rank_colours([(100.0, 3), (70.0, 3), (40.0, 3)]) == [pages.C.bgreen, pages.C.byellow, pages.C.bred]
	# n = 5: k = 2, top two green, bottom two red, middle yellow
	assert pages.rank_colours([(100.0, 3), (80.0, 3), (60.0, 3), (40.0, 3), (20.0, 3)]) == [pages.C.bgreen, pages.C.bgreen, pages.C.byellow, pages.C.bred, pages.C.bred]
	# n = 6: k = 2, top two green, bottom two red, middle two yellow
	assert pages.rank_colours([(100.0, 3), (85.0, 3), (70.0, 3), (55.0, 3), (40.0, 3), (25.0, 3)]) == [pages.C.bgreen, pages.C.bgreen, pages.C.byellow, pages.C.byellow, pages.C.bred, pages.C.bred]
	# a near-tie: within 5% of the fastest is green whatever its rank (611, 613, 600 are all green)
	assert pages.rank_colours([(611.0, 3), (613.0, 3), (600.0, 3)]) == [pages.C.bgreen, pages.C.bgreen, pages.C.bgreen]
	# near-ties share a colour: 487 is within 5% of 489, so both land in the fastest cluster and both come out green
	assert pages.rank_colours([(489.0, 3), (487.0, 3)]) == [pages.C.bgreen, pages.C.bgreen]
	# 613/611/600 are one near-tie cluster under a 1239 best: all three take the cluster's (2nd) rank colour, all yellow
	assert pages.rank_colours([(613.0, 3), (611.0, 3), (600.0, 3), (1239.0, 3)]) == [pages.C.byellow, pages.C.byellow, pages.C.byellow, pages.C.bgreen]
	# cells with fewer than 3 turns stay grey, whatever their value, and an empty cell is grey
	assert pages.rank_colours([(100.0, 3), (50.0, 3), (90.0, 2), (80.0, 1)]) == [pages.C.bgreen, pages.C.bred, pages.C.grey, pages.C.grey]
	assert pages.rank_colours([(100.0, 3), (nan, 0)]) == ["", pages.C.grey]


def test_prompt_table_has_no_cached_column():
	r1 = dict(req(preset="alpha", pn=500, pp=400.0, tn=40, tg=50.0, wall=1000.0), slot=0, cached=2000, kv=2540, acc=0.88)
	c = collector([r1], cfg=dict(CFG))
	view = pages.View()
	lines = plain(pages.requests(c, {"t": 2000.0}, view, 200, 80))
	i = next(i for i, ln in enumerate(lines) if "every prompt" in ln)
	header = lines[i + 2]  # the legend, then the header
	assert "cached" not in header, header
	assert "context" in header and "new" in header and "hit" in header, header


def test_prompt_row_compared_turn_by_turn():
	# a prompt of two turns, each exactly at its own bucket median but at different depths: the turn-by-turn
	# comparison is 1.0 (green), not the blue the old final-depth comparison would give
	a = dict(req(preset="alpha", pn=500, pp=400.0, tn=40, tg=50.0, wall=1000.0), slot=0, cached=0, kv=540)
	b = dict(req(preset="alpha", pn=500, pp=200.0, tn=40, tg=50.0, wall=1005.0), slot=0, cached=9000, kv=9540)
	# filler turns on other slots so each bucket has a 3+ turn median: 400 (<8k) and 200 (8-32k)
	f1 = dict(req(preset="alpha", pn=500, pp=400.0, tn=40, tg=50.0, wall=0.0), slot=1, cached=0, kv=540)
	f2 = dict(req(preset="alpha", pn=500, pp=400.0, tn=40, tg=50.0, wall=1.0), slot=1, cached=0, kv=540)
	f3 = dict(req(preset="alpha", pn=500, pp=200.0, tn=40, tg=50.0, wall=2.0), slot=2, cached=9000, kv=9540)
	f4 = dict(req(preset="alpha", pn=500, pp=200.0, tn=40, tg=50.0, wall=3.0), slot=2, cached=9000, kv=9540)
	c = collector([a, b, f1, f2, f3, f4], cfg=dict(CFG))
	snap = {"t": 2000.0}
	view = pages.View()
	st = pages.bucket_stats(c, view, snap)
	ps = pages.prompt_groups(c, view, snap)[0]
	p = next(p for p in ps if p["turns"][0] is a)  # the two-turn prompt
	pw, drop, _notes = pages.prompt_layout(200, ["alpha"], True)
	line = pages.prompt_row(c, p, st, True, pw=pw, drop=drop)
	assert pages.C.bgreen in line, line
	assert pages.C.bcyan not in line, line  # not blue: each turn is at its own bucket median


def test_prompt_groups_recomputes_only_on_new_requests():
	r1 = dict(req(preset="alpha", pn=500, pp=400.0, tn=40, tg=50.0, wall=1000.0), slot=0, cached=2000, kv=2540)
	r2 = dict(req(preset="alpha", pn=300, pp=420.0, tn=30, tg=52.0, wall=1005.0), slot=0, cached=2540, kv=2870)
	c = collector([r1, r2], cfg=dict(CFG))
	c.req_seq = 1
	view = pages.View()
	orig = pages.prompts.group
	calls = [0]

	def counting(*args, **kwargs):
		calls[0] += 1
		return orig(*args, **kwargs)

	pages.prompts.group = counting
	try:
		for sec in range(10):
			snap = {"t": float(sec)}
			pages.series(c, view, snap, "vram", 100)  # the Live page draws its charts here, resetting view.cache each second
			pages.prompt_groups(c, view, snap)
		assert calls[0] == 1, calls  # ten different seconds, no new request: the grouping survives the per-second reset
		c.req_seq = 2
		pages.prompt_groups(c, view, {"t": 99.0})
		assert calls[0] == 2, calls  # a new request re-triggers the grouping
	finally:
		pages.prompts.group = orig


def test_live_shows_recent_prompts_when_toggled():
	r1 = dict(req(preset="alpha", pn=500, pp=400.0, tn=40, tg=50.0, wall=1000.0), slot=0, cached=2000, kv=2540)
	r2 = dict(req(preset="alpha", pn=300, pp=420.0, tn=30, tg=52.0, wall=1005.0), slot=0, cached=2540, kv=2870)
	c = collector([r1, r2], cfg=dict(CFG))
	snap = live_snap([])
	view = pages.View()
	base = plain(pages.live(c, snap, view, 120, 40))
	assert any("recent turns" in ln for ln in base), base
	assert not any("recent prompts" in ln for ln in base), base
	view.live_prompts = True
	toggled = plain(pages.live(c, snap, view, 120, 40))
	assert any("recent prompts" in ln for ln in toggled), toggled
	assert not any("recent turns" in ln for ln in toggled), toggled


class _OrderedAlerts:
	def __init__(self, items):
		self.items = items

	def ordered(self):
		return self.items


def _alert(level, msg, since=990.0):
	return {"level": level, "msg": msg, "since": since}


def test_banner_one_line_per_alert_at_each_width():
	items = [
		("a", _alert("crit", "router unreachable at http://127.0.0.1:8080")),
		("b", _alert("warn", "shared 1.5 GiB, 300 MiB beyond expected")),
		("c", _alert("info", "loading model-a")),
	]
	c = collector([])
	c.alerts = _OrderedAlerts(items)
	for W in (80, 100, 140):
		lines = plain(pages.banner(c, {"t": 1000.0}, 4, W))
		assert len(lines) == 3, (W, lines)  # one line per alert, never wrapped
		assert "router unreachable at http://127.0.0.1:8080" in lines[0], lines
		assert "shared 1.5 GiB, 300 MiB beyond expected" in lines[1], lines
		assert "loading model-a" in lines[2], lines
		assert not any(ln.rstrip().endswith("…") for ln in lines), lines


def test_banner_caps_alerts_with_more_line():
	items = [(k, _alert("warn", "alert number %d" % i)) for i, k in enumerate("abcdef")]
	c = collector([])
	c.alerts = _OrderedAlerts(items)
	lines = plain(pages.banner(c, {"t": 1000.0}, 4, 100))
	assert len(lines) == 5, lines  # four alerts plus the "more" line
	assert "… 2 more alert(s)" in lines[4], lines


def test_events_page_detail_toggle():
	c = collector([])
	c.events = [
		{"wall": 1000.0, "level": "warn", "kind": "alert", "msg": "shared 1.5 GiB, 300 MiB beyond expected",
		 "detail": "up 300 MiB from the 1.0 GiB it held once the model was resident (lowest seen since); expected 525 MiB (measured with no VRAM pressure). Resident VRAM 4.0 GiB is 64 MiB under the OS budget 8.0 GiB"},
		{"wall": 1001.0, "level": "info", "kind": "loaded", "msg": "model-a ready in 2.5 s"},
	]
	snap = {"t": 1002.0, "notices": []}
	view = pages.View()
	off = "\n".join(plain(pages.events(c, snap, view, 100, 40)))
	assert "(f: filter, d: detail)" in off, off  # the section title names the keys
	assert "shared 1.5 GiB, 300 MiB beyond expected" in off, off
	assert "model-a ready in 2.5 s" in off, off
	assert "up 300 MiB from" not in off, off  # the detail is hidden by default

	view.detail = True
	rows = plain(pages.events(c, snap, view, 100, 40))
	detail_lines = [ln for ln in rows if ln.startswith("    ")]  # the indented continuation lines
	assert len(detail_lines) >= 2, rows  # the long detail wraps to several lines
	assert any("up 300 MiB from" in ln for ln in detail_lines), rows
	assert "model-a ready in 2.5 s" in "\n".join(rows), rows
	raw = pages.events(c, snap, view, 100, 40)
	assert any(pages.C.grey in ln for ln in raw if ln.startswith("    ")), raw  # the detail lines are dim




def make_test_snap(ll_kw=None, gpu_kw=None):
	g = {"name": "Test GPU", "vram": 24000.0, "budget": 20000.0, "ded": 17000.0, "shr": 2000.0, "uma": False}
	if gpu_kw:
		g.update(gpu_kw)
	ll = {
		"present": True, "pids": [2000], "ded": 16000.0, "shr": 1735.0, "com": 16000.0, "ws": 1024.0, "pws": 512.0, "commit": 1100.0,
		"age": 1000.0, "floor": 525.0, "floor_known": True, "allow": 525.0, "allow_known": True, "allow_src": "measured",
		"over": 1210.0, "ws_drop": 0.0, "commit_drop": 0.0, "vm": None, "vm_error": None,
		"eng": {"3D": 50.0},
	}
	if ll_kw:
		ll.update(ll_kw)
	return {
		"t": 1000.0, "router_up": True, "router_error": "", "router_auth_error": None, "presets": {},
		"gpu": g, "llama": ll,
		"ram": {"total": 65536.0, "avail": 32768.0, "commit": 20000.0, "commit_limit": 81920.0, "commit_max": 98304.0, "pagefiles": []},
		"others": [], "busy": False, "new_events": [], "notices": [],
	}


def test_shared_row_measured_and_overage():
	snap = make_test_snap({"shr": 1735.0, "allow": 525.0, "allow_known": True, "allow_src": "measured", "over": 1210.0})
	view = pages.View()
	text = "\n".join(plain(pages.gpu_block(collector([], cfg=dict(S.CFG)), snap, view, 140, 2)))
	assert "llama-server shared" in text, text
	assert "525 MiB expected" in text, text
	assert "+1,210 MiB beyond" in text, text


def test_shared_row_projected_expectation():
	snap = make_test_snap({"shr": 1735.0, "allow": 525.0, "allow_known": True, "allow_src": "projected", "over": 1210.0})
	view = pages.View()
	text = "\n".join(plain(pages.gpu_block(collector([], cfg=dict(S.CFG)), snap, view, 140, 2)))
	assert "llama-server shared" in text, text
	assert "525 MiB projected" in text, text
	assert "+1,210 MiB beyond" in text, text


def test_shared_row_unknown_expectation():
	snap = make_test_snap({"shr": 1735.0, "allow": 0.0, "allow_known": False, "allow_src": "", "over": 0.0})
	view = pages.View()
	text = "\n".join(plain(pages.gpu_block(collector([], cfg=dict(S.CFG)), snap, view, 140, 2)))
	assert "llama-server shared" in text, text
	assert "expected: unknown" in text, text


def test_shared_row_over_zero_no_beyond_term():
	snap = make_test_snap({"shr": 525.0, "allow": 525.0, "allow_known": True, "allow_src": "measured", "over": 0.0})
	view = pages.View()
	text = "\n".join(plain(pages.gpu_block(collector([], cfg=dict(S.CFG)), snap, view, 140, 2)))
	assert "llama-server shared" in text, text
	assert "525 MiB expected" in text, text
	assert "beyond" not in text, text


def test_no_percent_of_all_shared_anywhere():
	snap = make_test_snap({"shr": 1735.0, "allow": 525.0, "allow_known": True, "allow_src": "measured", "over": 1210.0, "ws": 1024.0, "pws": 512.0, "commit": 2048.0, "cpu": 1.0, "vm": None}, {"shr": 2000.0})
	view = pages.View()
	# Live row
	live_text = "\n".join(plain(pages.gpu_block(collector([], cfg=dict(S.CFG)), snap, view, 140, 2)))
	assert "of all shared" not in live_text, live_text
	# Memory page
	from llmtop_lib import state
	c = collector([], cfg=dict(state.CFG))
	m = {
		"name": "test-model", "arch": "llama", "n_layer": 1, "layers": {0: {"kind": "full", "mib": 100.0, "gpu": 100.0}}, "verified": True,
		"ctx": 4096, "cache_k": "q8_0", "cache_v": "q8_0",
		"checks": [], "assumed": [], "groups": {},
		"kv": {"layers": {"full": 0, "swa": 0}, "gpu": 0, "mtp": 0, "full": 0, "swa": 0, "per_token": 0},
		"rec": {"total": 0, "layers": 0, "per_layer": 0},
		"mtp_mib": 0, "draft": None, "fit": None,
		"weights": {"cpu": 0.0}, "roster": [], "ckpt_max": 0, "cache_ram": 0,
		"n_expert": 0, "n_expert_used": 0,
	}
	c.mem = {"test-model": {"state": "ready", "map": m, "key": "k"}}
	c.flags = {"test-model": ({}, "")}
	snap_mem = dict(snap, presets={"test-model": {"status": "loaded"}})
	mem_text = "\n".join(plain(pages.memory(c, snap_mem, view, 140, 40)))
	assert "of all shared" not in mem_text, mem_text
	assert "525 MiB expected" in mem_text, mem_text
	assert "+1,210 MiB beyond" in mem_text, mem_text
	# Alert detail
	from llmtop_lib import state
	a = state.Alerts(dict(state.CFG, router="http://127.0.0.1:8080"), lambda *args, **kw: None)
	a.evaluate(snap, None)
	self_alert = a.active.get("spill")
	assert self_alert is not None, a.active
	assert "of all shared" not in (self_alert.get("detail") or ""), self_alert


def test_shared_row_widths_live_and_memory():
	base_snap = {
		"t": 1000.0, "router_up": True, "router_error": "", "router_auth_error": None,
		"presets": {},
		"gpu": {"name": "Test GPU", "vram": 24564.0, "budget": 20000.0, "ded": 17000.0, "shr": 1877.0, "uma": False},
		"llama": {
			"present": True, "pids": [2000], "ded": 16000.0, "shr": 1877.0, "com": 16000.0, "ws": 1024.0, "pws": 512.0, "commit": 1100.0,
			"age": 1000.0, "floor": 1877.0, "floor_known": True, "allow": 525.0, "allow_known": True, "allow_src": "measured",
			"over": 1352.0, "ws_drop": 0.0, "commit_drop": 0.0, "vm": None, "vm_error": None,
			"eng": {"3D": 50.0}, "cpu": 1.0,
		},
		"ram": {"total": 65536.0, "avail": 32768.0, "standby": 16384.0, "commit": 20000.0, "commit_limit": 81920.0, "commit_max": 98304.0, "pagefiles": [], "pages_in": 0.0, "pages_out": 0.0},
		"cpu": {"pct": 10.0, "cores": 16, "threads": 32, "freq": 3500.0, "top": []},
		"others": [], "busy": False, "new_events": [], "notices": [],
	}

	widths = [80, 100, 120, 140, 180]
	for page_idx, page_name in [(0, "Live"), (1, "Memory")]:
		view = pages.View()
		view.page = page_idx
		snap_over = dict(base_snap)
		if page_name == "Memory":
			snap_over["presets"] = {"test-model": {"status": "loaded"}}
		snap_zero = dict(snap_over, llama=dict(snap_over["llama"], shr=525.0, floor=525.0, over=0.0))

		# 1. With overage fixture: line with beyond figure has no '…' and contains '+1,352 MiB beyond'
		for w in widths:
			c = collector([], cfg=dict(S.CFG))
			c.alerts.ordered = lambda: []
			m = {
				"name": "test-model", "arch": "llama", "n_layer": 1, "layers": {0: {"kind": "full", "mib": 100.0, "gpu": 100.0}}, "verified": True,
				"ctx": 4096, "cache_k": "q8_0", "cache_v": "q8_0",
				"checks": [], "assumed": [], "groups": {},
				"kv": {"layers": {"full": 0, "swa": 0}, "gpu": 0, "mtp": 0, "full": 0, "swa": 0, "per_token": 0},
				"rec": {"total": 0, "layers": 0, "per_layer": 0},
				"mtp_mib": 0, "draft": None, "fit": None,
				"weights": {"cpu": 0.0}, "roster": [], "ckpt_max": 0, "cache_ram": 0,
				"n_expert": 0, "n_expert_used": 0,
			}
			c.mem = {"test-model": {"state": "ready", "map": m, "key": "k"}}
			c.flags = {"test-model": ({}, "")}
			rendered = pages.render(c, snap_over, view, w, 40)
			beyond_lines = [ln for ln in rendered if "+1,352 MiB beyond" in ln]
			assert len(beyond_lines) == 1, (page_name, w, beyond_lines)
			ln = beyond_lines[0]
			assert "…" not in ln, (page_name, w, ln)
			assert "+1,352 MiB beyond" in ln, (page_name, w, ln)

		# 2. With over == 0: no 'beyond' term
		for w in widths:
			c = collector([], cfg=dict(S.CFG))
			c.alerts.ordered = lambda: []
			m = {
				"name": "test-model", "arch": "llama", "n_layer": 1, "layers": {0: {"kind": "full", "mib": 100.0, "gpu": 100.0}}, "verified": True,
				"ctx": 4096, "cache_k": "q8_0", "cache_v": "q8_0",
				"checks": [], "assumed": [], "groups": {},
				"kv": {"layers": {"full": 0, "swa": 0}, "gpu": 0, "mtp": 0, "full": 0, "swa": 0, "per_token": 0},
				"rec": {"total": 0, "layers": 0, "per_layer": 0},
				"mtp_mib": 0, "draft": None, "fit": None,
				"weights": {"cpu": 0.0}, "roster": [], "ckpt_max": 0, "cache_ram": 0,
				"n_expert": 0, "n_expert_used": 0,
			}
			c.mem = {"test-model": {"state": "ready", "map": m, "key": "k"}}
			c.flags = {"test-model": ({}, "")}
			rendered = pages.render(c, snap_zero, view, w, 40)
			assert not any("beyond" in ln for ln in rendered), (page_name, w, [ln for ln in rendered if "beyond" in ln])


class DataRing:
	"""A ring that returns a varying series so charts render partial and full cells, not just blanks."""

	def cols(self, t, seconds, n, how="max"):
		if n <= 0:
			return []
		return [((i * 37) % 100) / 100.0 * 8.0 for i in range(n)]

	def span_max(self, a, b):
		return 8.0


class DataRings:
	def __getitem__(self, key):
		return DataRing()


def _coarse_allowed():
	"""Everything the classic console draws: ASCII, all of code page 437, plus the symbols the pages already use outside
	charts and bars that we assume the console draws (they are not in cp437)."""
	allowed = set(range(128))
	for b in range(256):
		try:
			allowed.add(ord(bytes([b]).decode("cp437")))
		except Exception:
			pass
	# outside-chart symbols the pages already use, not in cp437 (assumed to draw on the user's console):
	# ● ▲ ◆ ┃ … ◂ ▸ ✓ ↑ ↓ ← → the en dash in the chart axis, ≈ in the memory page's text, × in the cache topology
	for cp in (0x25CF, 0x25B2, 0x25C6, 0x2503, 0x2026, 0x25C2, 0x25B8, 0x2713, 0x2191, 0x2193, 0x2190, 0x2192, 0x2013, 0x2248, 0x00D7):
			allowed.add(cp)
	return allowed


def _full_snap():
	snap = make_test_snap()
	snap["ram"]["standby"] = 16384.0
	snap["ram"]["pages_in"] = 0.0
	snap["ram"]["pages_out"] = 0.0
	snap["ram"]["free"] = 8192.0
	snap["ram"]["modified"] = 4096.0
	snap["ram"]["ws_total"] = 30000.0
	snap["ram"]["ws_by"] = {}
	snap["ram"]["pf_cfg"] = []
	snap["cpu"] = {"pct": 10.0, "cores": 8, "threads": 16, "freq": 3500.0, "top": [], "cpu_cores": [10.0, 20.0, 55.0, 90.0, 5.0, 35.0, 70.0, 45.0]}
	snap["llama"]["cpu"] = 1.0
	return snap


def _coarse_collector():
	c = collector([], cfg=dict(S.CFG))
	c.rings = DataRings()
	c.cpu_info = {}
	c.alerts.ordered = lambda: []
	return c


def test_coarse_pages_only_allowed_glyphs():
	allowed = _coarse_allowed()
	old = ui.STYLE["mode"]
	try:
		ui.STYLE["mode"] = "coarse"
		snap = _full_snap()
		for page_idx in range(6):
			view = pages.View()
			view.page = page_idx
			c = _coarse_collector()
			rendered = pages.render(c, snap, view, 120, 40)
			for ln in rendered:
				plain_ln = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", ln)
				bad = [ch for ch in plain_ln if ord(ch) not in allowed]
				assert not bad, ("page %d" % page_idx, sorted(set(bad)), plain_ln)
	finally:
		ui.STYLE["mode"] = old


def test_bar_fractions_all_styles():
	old = ui.STYLE["mode"]
	try:
		for style in ("braille", "blocks", "coarse", "ascii"):
			ui.STYLE["mode"] = style
			t = ui.TABLES[style]
			full_g, empty_g = t["full"], t["empty"]
			out0 = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", ui.bar(0.0, 10))
			assert out0 == empty_g * 10, (style, out0)
			out1 = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", ui.bar(1.0, 10))
			assert out1 == full_g * 10, (style, out1)
			out_half = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", ui.bar(0.5, 10))
			assert out_half == full_g * 5 + empty_g * 5, (style, out_half)
	finally:
		ui.STYLE["mode"] = old


def test_ascii_charts_and_bars_printable_ascii():
	old = ui.STYLE["mode"]
	try:
		ui.STYLE["mode"] = "ascii"

		def asc(s):
			return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s)

		def is_asciiprint(s):
			return all(32 <= ord(ch) <= 126 for ch in s)

		for frac in (0.0, 0.3, 0.5, 0.7, 1.0):
			out = asc(ui.bar(frac, 20))
			assert is_asciiprint(out), (frac, out)
		out = asc(ui.stack([(5, ""), (3, "")], 10, 20, marker=7))
		assert is_asciiprint(out), out
		vals = [1.0, 3.0, 2.0, 5.0, 4.0, 2.5, 0.5]
		for ln in ui.plot(vals, 0.0, 5.0, 3):
			out = asc(ln)
			assert is_asciiprint(out), out
		for u in range(0, 101, 5):
			g = ui.core_glyph(u)
			assert is_asciiprint(g), (u, g)
	finally:
		ui.STYLE["mode"] = old


def test_pick_model_cycles_over_flagless_model():
	c = collector([], cfg=dict(S.CFG))
	c.flags = {}
	c.mem = {}
	def ensure_map(name):
		if name not in c.flags:
			c.mem[name] = {"key": None, "state": "error", "error": "the server did not report this model's command line (/models has no status.args) and no llama-server process was found for it"}
	c.ensure_map = ensure_map
	snap = {"t": 1000.0, "presets": {"flagless": {"status": "loaded"}}}
	view = pages.View()
	name, order, loaded = pages.pick_model(c, snap, view)
	assert name == "flagless" and order == ["flagless"] and loaded == ["flagless"], (name, order, loaded)
	assert c.mem["flagless"]["state"] == "error"
	view.msel = 1  # m wraps around; the flagless model is still listed and reachable
	assert pages.pick_model(c, snap, view)[0] == "flagless"


def test_memory_page_flagless_model_shows_could_not_analyse():
	c = collector([], cfg=dict(S.CFG))
	c.flags = {}
	c.mem = {}
	def ensure_map(name):
		if name not in c.flags:
			c.mem[name] = {"key": None, "state": "error", "error": "the server did not report this model's command line (/models has no status.args) and no llama-server process was found for it"}
	c.ensure_map = ensure_map
	snap = {
		"t": 1000.0, "router_up": True, "mode": "router",
		"presets": {"flagless": {"status": "loaded"}},
		"gpu": {"name": "Test GPU", "vram": 24564.0, "budget": 20000.0, "ded": 0.0, "shr": 0.0, "uma": False},
		"llama": {"present": False, "ded": 0.0, "shr": 0.0},
	}
	view = pages.View()
	view.page = 1
	text = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in pages.memory(c, snap, view, 120, 40))
	assert "could not analyse" in text, text
	assert "no status.args" in text, text


def test_tensors_page_flagless_model_shows_could_not_analyse():
	c = collector([], cfg=dict(S.CFG))
	c.flags = {}
	c.mem = {}
	def ensure_map(name):
		if name not in c.flags:
			c.mem[name] = {"key": None, "state": "error", "error": "the server did not report this model's command line (/models has no status.args) and no llama-server process was found for it"}
	c.ensure_map = ensure_map
	snap = {"t": 1000.0, "router_up": True, "mode": "router", "presets": {"flagless": {"status": "loaded"}}}
	view = pages.View()
	view.page = 5
	text = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in pages.tensors(c, snap, view, 120, 40))
	assert "could not analyse" in text, text
	assert "no status.args" in text, text


def rendered_live_snap(p):
	return {
		"t": 1000.0, "router_up": True, "router_error": "", "router_auth_error": None,
		"presets": {"m": p},
		"gpu": {"name": "Test GPU", "vram": 24564.0, "budget": 20000.0, "ded": 17000.0, "shr": 1877.0, "uma": False},
		"llama": {
			"present": True, "pids": [2000], "ded": 16000.0, "shr": 1877.0, "com": 16000.0, "ws": 1024.0, "pws": 512.0, "commit": 1100.0,
			"age": 1000.0, "floor": 1877.0, "floor_known": True, "allow": 525.0, "allow_known": True, "allow_src": "measured",
			"over": 0.0, "ws_drop": 0.0, "commit_drop": 0.0, "vm": None, "vm_error": None,
			"eng": {"3D": 50.0}, "cpu": 1.0,
		},
		"ram": {"total": 65536.0, "avail": 32768.0, "standby": 16384.0, "commit": 20000.0, "commit_limit": 81920.0, "commit_max": 98304.0, "pagefiles": [], "pages_in": 0.0, "pages_out": 0.0},
		"cpu": {"pct": 10.0, "cores": 16, "threads": 32, "freq": 3500.0, "top": []},
		"others": [], "busy": False, "new_events": [], "notices": [],
	}


def test_model_block_three_slots_only_slot_2_processing():
	c = collector([], cfg=dict(S.CFG))
	c.alerts.ordered = lambda: []
	p = {
		"meta": {}, "status": "loaded", "phase": "decode", "kv": 150,
		"rate_dec": 25.5, "rate_pre": float("nan"), "eta": float("nan"),
		"elapsed": 2.0, "n_slots": 3, "n_busy": 1,
		"slot": {
			"id": 2, "n_ctx": 8192, "task": 42, "n_prompt": 100, "cached": 0,
			"done": 100, "todo": 100, "decoded": 50, "remain": 150, "params": {},
			"speculative": False,
		},
	}
	snap = rendered_live_snap(p)
	for w in (80, 120, 200):
		view = pages.View()
		rendered = pages.render(c, snap, view, w, 40)
		for ln in rendered:
			assert ui.vlen(ln) <= w, (w, ln, ui.vlen(ln))
		slots_lines = [ln for ln in rendered if "slots" in ln and "busy" in ln]
		assert len(slots_lines) == 1, (w, slots_lines)
		assert "1 of 3 busy" in slots_lines[0], (w, slots_lines[0])
		assert "showing slot 2" in slots_lines[0], (w, slots_lines[0])
		assert "…" not in slots_lines[0], (w, slots_lines[0])
		lines = pages.model_block(c, snap, view, "m", p, w, 2)
		assert ui.vlen(lines[2]) <= w, (w, lines[2], ui.vlen(lines[2]))
		assert "1 of 3 busy" in lines[2], (w, lines[2])
		assert "showing slot 2" in lines[2], (w, lines[2])
		assert "…" not in lines[2], (w, lines[2])
		assert "busy" not in plain(lines)[1], (w, lines[1])


def test_model_block_two_slots_processing():
	c = collector([], cfg=dict(S.CFG))
	c.alerts.ordered = lambda: []
	p = {
		"meta": {}, "status": "loaded", "phase": "decode", "kv": 150,
		"rate_dec": 25.5, "rate_pre": float("nan"), "eta": float("nan"),
		"elapsed": 2.0, "n_slots": 3, "n_busy": 2,
		"slot": {
			"id": 1, "n_ctx": 8192, "task": 43, "n_prompt": 100, "cached": 0,
			"done": 100, "todo": 100, "decoded": 50, "remain": 150, "params": {},
			"speculative": False,
		},
	}
	snap = rendered_live_snap(p)
	for w in (80, 120, 200):
		view = pages.View()
		rendered = pages.render(c, snap, view, w, 40)
		for ln in rendered:
			assert ui.vlen(ln) <= w, (w, ln, ui.vlen(ln))
		slots_lines = [ln for ln in rendered if "slots" in ln and "busy" in ln]
		assert len(slots_lines) == 1, (w, slots_lines)
		assert "2 of 3 busy" in slots_lines[0], (w, slots_lines[0])
		assert "showing slot 1" in slots_lines[0], (w, slots_lines[0])
		assert "…" not in slots_lines[0], (w, slots_lines[0])
		lines = pages.model_block(c, snap, view, "m", p, w, 2)
		assert ui.vlen(lines[2]) <= w, (w, lines[2], ui.vlen(lines[2]))
		assert "2 of 3 busy" in lines[2], (w, lines[2])
		assert "showing slot 1" in lines[2], (w, lines[2])
		assert "…" not in lines[2], (w, lines[2])
		assert "busy" not in plain(lines)[1], (w, lines[1])


def test_model_block_unified_cache_total_tokens():
	c = collector([], cfg=dict(S.CFG))
	c.alerts.ordered = lambda: []
	p = {
		"meta": {}, "status": "loaded", "phase": "decode", "kv": 5000,
		"rate_dec": 25.5, "rate_pre": float("nan"), "eta": float("nan"),
		"elapsed": 2.0, "n_slots": 2, "n_busy": 1, "all_tokens": 12345,
		"slot": {
			"id": 0, "n_ctx": 65536, "task": 1, "n_prompt": 5000, "cached": 0,
			"done": 5000, "todo": 5000, "decoded": 50, "remain": 150, "params": {},
			"speculative": False,
		},
	}
	snap = rendered_live_snap(p)
	for w in (80, 120, 200):
		view = pages.View()
		rendered = pages.render(c, snap, view, w, 40)
		for ln in rendered:
			assert ui.vlen(ln) <= w, (w, ln, ui.vlen(ln))
		slots_lines = [ln for ln in rendered if "slots" in ln and "busy" in ln]
		assert len(slots_lines) == 1, (w, slots_lines)
		assert "1 of 2 busy" in slots_lines[0], (w, slots_lines[0])
		assert "showing slot 0" in slots_lines[0], (w, slots_lines[0])
		assert "12,345 tokens in the shared cache" in slots_lines[0], (w, slots_lines[0])
		assert "…" not in slots_lines[0], (w, slots_lines[0])
		lines = pages.model_block(c, snap, view, "m", p, w, 2)
		assert ui.vlen(lines[2]) <= w, (w, lines[2], ui.vlen(lines[2]))
		assert "1 of 2 busy" in lines[2], (w, lines[2])
		assert "showing slot 0" in lines[2], (w, lines[2])
		assert "12,345 tokens in the shared cache" in lines[2], (w, lines[2])
		assert "…" not in lines[2], (w, lines[2])
		assert "all slots" not in plain(lines)[3], (w, lines[3])


def test_model_block_one_slot_renders_identically_to_baseline():
	c = collector([])
	snap = {"t": 1000.0, "llama": {"ded": 1000.0, "present": False}}
	slot_data = {
		"id": 0, "n_ctx": 8192, "task": 42, "n_prompt": 100, "cached": 0,
		"done": 100, "todo": 100, "decoded": 50, "remain": 150, "params": {},
		"speculative": False,
	}
	p_one = {
		"meta": {}, "status": "loaded", "phase": "decode", "kv": 150,
		"rate_dec": 25.5, "rate_pre": float("nan"), "eta": float("nan"),
		"elapsed": 2.0, "n_slots": 1, "n_busy": 1, "slot": slot_data,
	}
	p_base = {
		"meta": {}, "status": "loaded", "phase": "decode", "kv": 150,
		"rate_dec": 25.5, "rate_pre": float("nan"), "eta": float("nan"),
		"elapsed": 2.0, "n_slots": 0, "n_busy": 0, "slot": slot_data,
	}
	for w in (80, 120, 200):
		lines_one = pages.model_block(c, snap, pages.View(), "m", p_one, w, 2)
		lines_base = pages.model_block(c, snap, pages.View(), "m", p_base, w, 2)
		assert lines_one == lines_base, "One-slot server lines differ from baseline at w=%d:\n%s\nvs\n%s" % (w, lines_one, lines_base)
		assert not any("slots" in ln and "busy" in ln for ln in lines_one), (w, lines_one)


def test_model_block_slot_line_shortens_terms_when_tight():
	c = collector([], cfg=dict(S.CFG))
	c.alerts.ordered = lambda: []
	p = {
		"meta": {}, "status": "loaded", "phase": "decode", "kv": 5000,
		"rate_dec": 25.5, "rate_pre": float("nan"), "eta": float("nan"),
		"elapsed": 2.0, "n_slots": 16, "n_busy": 16, "all_tokens": 99999999,
		"slot": {
			"id": 15, "n_ctx": 65536, "task": 42, "n_prompt": 5000, "cached": 0,
			"done": 5000, "todo": 5000, "decoded": 50, "remain": 150, "params": {},
			"speculative": False,
		},
	}
	snap = rendered_live_snap(p)
	for w in (80, 120, 200):
		view = pages.View()
		rendered = pages.render(c, snap, view, w, 40)
		for ln in rendered:
			assert ui.vlen(ln) <= w, (w, ln, ui.vlen(ln))
		slots_lines = [ln for ln in rendered if "slots" in ln and "busy" in ln]
		assert len(slots_lines) == 1, (w, slots_lines)
		assert "16 of 16 busy" in slots_lines[0], (w, slots_lines[0])
		assert "showing slot 15" in slots_lines[0], (w, slots_lines[0])
		assert "…" not in slots_lines[0], (w, slots_lines[0])
		if w == 80:
			assert "tokens in shared cache" in slots_lines[0], (w, slots_lines[0])
		else:
			assert "tokens in the shared cache" in slots_lines[0], (w, slots_lines[0])


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
	("requests_v2_colours_notes_and_nested_turns", test_requests_v2_colours_notes_and_nested_turns),
	("prompt_and_turn_rows_have_hit_and_draft", test_prompt_and_turn_rows_have_hit_and_draft),
	("requests_fit_width_100", test_requests_fit_width_100),
	("by_context_tables_ranked_colours", test_by_context_tables_ranked_colours),
	("effect_section_removed", test_effect_section_removed),
	("rank_colours", test_rank_colours),
	("prompt_table_has_no_cached_column", test_prompt_table_has_no_cached_column),
	("prompt_row_compared_turn_by_turn", test_prompt_row_compared_turn_by_turn),
	("prompt_groups_recomputes_only_on_new_requests", test_prompt_groups_recomputes_only_on_new_requests),
	("live_shows_recent_prompts_when_toggled", test_live_shows_recent_prompts_when_toggled),
	("banner_one_line_per_alert_at_each_width", test_banner_one_line_per_alert_at_each_width),
	("banner_caps_alerts_with_more_line", test_banner_caps_alerts_with_more_line),
	("events_page_detail_toggle", test_events_page_detail_toggle),
	("shared_row_measured_and_overage", test_shared_row_measured_and_overage),
	("shared_row_projected_expectation", test_shared_row_projected_expectation),
	("shared_row_unknown_expectation", test_shared_row_unknown_expectation),
	("shared_row_over_zero_no_beyond_term", test_shared_row_over_zero_no_beyond_term),
	("no_percent_of_all_shared_anywhere", test_no_percent_of_all_shared_anywhere),
	("shared_row_widths_live_and_memory", test_shared_row_widths_live_and_memory),
	("coarse_pages_only_allowed_glyphs", test_coarse_pages_only_allowed_glyphs),
	("bar_fractions_all_styles", test_bar_fractions_all_styles),
	("ascii_charts_and_bars_printable_ascii", test_ascii_charts_and_bars_printable_ascii),
	("pick_model_cycles_over_flagless_model", test_pick_model_cycles_over_flagless_model),
	("memory_page_flagless_model_shows_could_not_analyse", test_memory_page_flagless_model_shows_could_not_analyse),
	("tensors_page_flagless_model_shows_could_not_analyse", test_tensors_page_flagless_model_shows_could_not_analyse),
	("model_block_three_slots_only_slot_2_processing", test_model_block_three_slots_only_slot_2_processing),
	("model_block_two_slots_processing", test_model_block_two_slots_processing),
	("model_block_unified_cache_total_tokens", test_model_block_unified_cache_total_tokens),
	("model_block_one_slot_renders_identically_to_baseline", test_model_block_one_slot_renders_identically_to_baseline),
	("model_block_slot_line_shortens_terms_when_tight", test_model_block_slot_line_shortens_terms_when_tight),
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
