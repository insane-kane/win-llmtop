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
	assert "32+ new tokens" in text and "decode over 20+" in text, text


def test_requests_subtitle_single_cutoff():
	c = collector([req(preset="m", pn=2000, pp=500.0, tn=100, tg=30.0)], cfg=dict(CFG))
	text = "\n".join(plain_requests(c))
	assert "per preset" not in text, text
	assert "1,000+ new tokens" in text and "decode over 50+" in text, text


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
		("b", _alert("warn", "shared memory 1.5 GiB, 300 MiB over expected")),
		("c", _alert("info", "loading model-a")),
	]
	c = collector([])
	c.alerts = _OrderedAlerts(items)
	for W in (80, 100, 140):
		lines = plain(pages.banner(c, {"t": 1000.0}, 4, W))
		assert len(lines) == 3, (W, lines)  # one line per alert, never wrapped
		assert "router unreachable at http://127.0.0.1:8080" in lines[0], lines
		assert "shared memory 1.5 GiB, 300 MiB over expected" in lines[1], lines
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
		{"wall": 1000.0, "level": "warn", "kind": "alert", "msg": "shared memory 1.5 GiB, 300 MiB over expected",
		 "detail": "up 300 MiB from the 1.0 GiB it held once the model was resident. llama-server holds 5% of all shared GPU memory. Resident VRAM 4.0 GiB is 64 MiB under the OS budget 8.0 GiB"},
		{"wall": 1001.0, "level": "info", "kind": "loaded", "msg": "model-a ready in 2.5 s"},
	]
	snap = {"t": 1002.0, "notices": []}
	view = pages.View()
	off = "\n".join(plain(pages.events(c, snap, view, 100, 40)))
	assert "(f: filter, d: detail)" in off, off  # the section title names the keys
	assert "shared memory 1.5 GiB, 300 MiB over expected" in off, off
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
	("live_shows_recent_prompts_when_toggled", test_live_shows_recent_prompts_when_toggled),
	("banner_one_line_per_alert_at_each_width", test_banner_one_line_per_alert_at_each_width),
	("banner_caps_alerts_with_more_line", test_banner_caps_alerts_with_more_line),
	("events_page_detail_toggle", test_events_page_detail_toggle),
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
