#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for llmtop_lib/prompts.py: prompt grouping rules and time-weighted figures.

	python -I tools/win-llmtop/tests/test_prompts.py
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from llmtop_lib import prompts  # noqa: E402


def turn(preset="alpha", slot=0, session=0, wall0=100.0, wall=105.0, t0=10.0, t=15.0, pn=500, pms=1000.0, cached=1000, tn=50, tms=1000.0, kv=None, sel="LCP", keep=1.0, evicted=0, trunc=False, reallocs=0, ms=None, acc=None, dacc=None, dgen=None):
	if kv is None:
		kv = (cached or 0) + pn + tn
	if ms is None:
		ms = pms + tms
	r = {
		"preset": preset,
		"slot": slot,
		"session": session,
		"wall0": wall0,
		"wall": wall,
		"t0": t0,
		"t": t,
		"pn": pn,
		"pms": pms,
		"pp": 1000.0 * pn / pms if pms > 0 else float("nan"),
		"cached": cached,
		"tn": tn,
		"tms": tms,
		"tg": 1000.0 * tn / tms if tms > 0 else float("nan"),
		"kv": kv,
		"sel": sel,
		"keep": keep,
		"evicted": evicted,
		"trunc": trunc,
		"reallocs": reallocs,
		"ms": ms,
	}
	if acc is not None:
		r["acc"] = acc
	if dacc is not None:
		r["dacc"] = dacc
	if dgen is not None:
		r["dgen"] = dgen
	return r


def test_single_turn_prompt():
	t1 = turn(wall0=100.0, wall=102.5, pn=400, pms=800.0, cached=1000, tn=60, tms=1200.0, kv=1460)
	ps = prompts.group([t1], gap_s=30)
	assert len(ps) == 1
	p = ps[0]
	assert p["n"] == 1 and p["turns"] == [t1]
	assert p["wall0"] == 100.0 and p["wall"] == 102.5 and p["wall_s"] == 2.5
	assert p["kv"] == 1460 and p["pn"] == 400 and p["cached"] == 1000 and p["tn"] == 60
	assert abs(p["pp"] - 500.0) < 1e-6 and abs(p["tg"] - 50.0) < 1e-6
	assert p["ms"] == 2000.0 and p["issues"] == []


def test_gap_split():
	t1 = turn(wall0=100.0, wall=105.0)
	t2 = turn(wall0=134.9, wall=140.0)  # gap 29.9 s < 30 s -> same prompt
	t3 = turn(wall0=170.0, wall=175.0)  # gap 30.0 s >= 30 s -> new prompt
	ps = prompts.group([t1, t2, t3], gap_s=30)
	assert [p["n"] for p in ps] == [2, 1]
	assert ps[0]["wall0"] == 100.0 and ps[0]["wall"] == 140.0 and ps[0]["wall_s"] == 40.0
	assert ps[1]["wall0"] == 170.0 and ps[1]["wall"] == 175.0


def test_lru_split():
	t1 = turn(wall0=100.0, wall=105.0, sel="LRU", evicted=0)  # empty slot at start
	t2 = turn(wall0=106.0, wall=110.0, sel="LCP", keep=1.0)
	t3 = turn(wall0=111.0, wall=115.0, sel="LRU", evicted=2048)  # evicted > 0 -> new prompt
	t4 = turn(wall0=116.0, wall=120.0, sel="LRU", evicted=0)  # evicted == 0 -> does not split
	ps = prompts.group([t1, t2, t3, t4], gap_s=30)
	assert [p["n"] for p in ps] == [2, 2]
	assert ps[0]["issues"] == []
	assert ps[1]["issues"] == ["cache_reset"] and ps[1]["evicted"] == 2048


def test_keep_split():
	t1 = turn(wall0=100.0, wall=105.0, keep=1.0)
	t2 = turn(wall0=106.0, wall=110.0, keep=0.5)  # 0.5 >= 0.5 -> stays in prompt
	t3 = turn(wall0=111.0, wall=115.0, keep=0.49)  # < 0.5 -> new prompt
	ps = prompts.group([t1, t2, t3], gap_s=30)
	assert [p["n"] for p in ps] == [2, 1]


def test_reload_split():
	t1 = turn(session=0, wall0=100.0, wall=105.0)
	t2 = turn(session=0, wall0=106.0, wall=110.0)
	t3 = turn(session=1, wall0=112.0, wall=116.0)  # session changed -> new prompt even with 2 s gap
	ps = prompts.group([t1, t2, t3], gap_s=30)
	assert [p["n"] for p in ps] == [2, 1]
	assert ps[0]["session"] == 0 and ps[1]["session"] == 1


def test_two_slots_interleaved():
	s0_a = turn(slot=0, wall0=100.0, wall=104.0, pn=200, cached=1000, tn=20, kv=1220)
	s1_a = turn(slot=1, wall0=101.0, wall=105.0, pn=300, cached=2000, tn=30, kv=2330)
	s0_b = turn(slot=0, wall0=106.0, wall=110.0, pn=100, cached=1220, tn=40, kv=1360)
	s1_b = turn(slot=1, wall0=107.0, wall=112.0, pn=150, cached=2330, tn=50, kv=2530)
	ps = prompts.group([s0_a, s1_a, s0_b, s1_b], gap_s=30)
	assert len(ps) == 2
	assert ps[0]["slot"] == 0 and ps[0]["turns"] == [s0_a, s0_b] and ps[0]["kv"] == 1360 and ps[0]["pn"] == 300 and ps[0]["tn"] == 60
	assert ps[1]["slot"] == 1 and ps[1]["turns"] == [s1_a, s1_b] and ps[1]["kv"] == 2530 and ps[1]["pn"] == 450 and ps[1]["tn"] == 80


def test_missing_walls():
	# Case 1: walls are None, but relative timestamps t0/t are present in the same session
	a1 = turn(wall0=None, wall=None, t0=5.0, t=10.0)
	a2 = turn(wall0=None, wall=None, t0=20.0, t=25.0)  # gap 10 s < 30 s
	a3 = turn(wall0=None, wall=None, t0=60.0, t=65.0)  # gap 35 s >= 30 s -> split
	ps = prompts.group([a1, a2, a3], gap_s=30)
	assert [p["n"] for p in ps] == [2, 1]
	assert ps[0]["wall_s"] == 20.0

	# Case 2: neither walls nor timestamps are usable (t0=0, t=0 as in untimestamped logs) -> only cache rules apply
	b1 = turn(wall0=None, wall=None, t0=0.0, t=0.0, keep=1.0)
	b2 = turn(wall0=None, wall=None, t0=0.0, t=0.0, keep=1.0)
	b3 = turn(wall0=None, wall=None, t0=0.0, t=0.0, keep=0.2)  # cache rule splits
	ps2 = prompts.group([b1, b2, b3], gap_s=30)
	assert [p["n"] for p in ps2] == [2, 1]
	assert math.isnan(ps2[0]["wall_s"])


def test_time_weighted_rates_and_issues_union():
	# Turn 1: 1000 pn in 1000 ms (1000 t/s), 100 tn in 1000 ms (100 t/s)
	# Turn 2: 1000 pn in 4000 ms (250 t/s), 100 tn in 4000 ms (25 t/s)
	# Mean of rates would be (1000 + 250) / 2 = 625 t/s and (100 + 25) / 2 = 62.5 t/s.
	# Time-weighted Σpn / Σpms = 2000 / 5.0 s = 400 t/s, Σtn / Σtms = 200 / 5.0 s = 40 t/s.
	t1 = turn(wall0=100.0, wall=102.0, pn=1000, pms=1000.0, tn=100, tms=1000.0, trunc=True)
	t2 = turn(wall0=105.0, wall=113.0, pn=1000, pms=4000.0, tn=100, tms=4000.0, reallocs=2)
	ps = prompts.group([t1, t2], gap_s=30)
	assert len(ps) == 1
	p = ps[0]
	mean_pp = (t1["pp"] + t2["pp"]) / 2
	mean_tg = (t1["tg"] + t2["tg"]) / 2
	assert abs(mean_pp - 625.0) < 1e-6 and abs(p["pp"] - 400.0) < 1e-6
	assert abs(mean_tg - 62.5) < 1e-6 and abs(p["tg"] - 40.0) < 1e-6
	assert p["issues"] == ["trunc", "reallocs"]
	assert p["trunc"] is True and p["reallocs"] == 2


def test_hit_with_cache_reset_turn():
	# the prompt's first turn was an LRU pick that evicted the slot's held tokens, so it recomputed the whole prompt (cached 0)
	t1 = turn(wall0=100.0, wall=105.0, sel="LRU", evicted=2048, cached=0, pn=1500)
	t2 = turn(wall0=106.0, wall=110.0, cached=1500, pn=500)
	ps = prompts.group([t1, t2], gap_s=30)
	assert len(ps) == 1
	p = ps[0]
	assert p["issues"] == ["cache_reset"]
	assert abs(p["hit"] - 1500 / 3500) < 1e-9  # Σcached / Σ(cached + pn) over the turns


def test_hit_nan_without_cached():
	t1 = turn(wall0=100.0, wall=105.0, cached=None)
	ps = prompts.group([t1], gap_s=30)
	assert math.isnan(ps[0]["hit"])


def test_acc_sum_over_sum_not_mean_of_rates():
	# per-turn rates 0.9 and 0.2 (mean 0.55); Σdacc / Σdgen = 100 / 150
	t1 = turn(wall0=100.0, wall=105.0, acc=0.9, dacc=90, dgen=100, tn=100)
	t2 = turn(wall0=106.0, wall=110.0, acc=0.2, dacc=10, dgen=50, tn=10)
	ps = prompts.group([t1, t2], gap_s=30)
	p = ps[0]
	assert abs(p["acc"] - 100 / 150) < 1e-9
	assert abs(p["acc"] - (0.9 + 0.2) / 2) > 0.1


def test_acc_from_old_rows_weighted_by_tn():
	# older history: no draft counts, only the per-turn acceptance rates
	t1 = turn(wall0=100.0, wall=105.0, acc=0.9, tn=100)
	t2 = turn(wall0=106.0, wall=110.0, acc=0.2, tn=10)
	ps = prompts.group([t1, t2], gap_s=30)
	assert abs(ps[0]["acc"] - (0.9 * 100 + 0.2 * 10) / 110) < 1e-9


def test_acc_none_without_draft():
	ps = prompts.group([turn(wall0=100.0, wall=105.0)], gap_s=30)
	assert ps[0]["acc"] is None


CASES = [
	("single_turn_prompt", test_single_turn_prompt),
	("gap_split", test_gap_split),
	("lru_split", test_lru_split),
	("keep_split", test_keep_split),
	("reload_split", test_reload_split),
	("two_slots_interleaved", test_two_slots_interleaved),
	("missing_walls", test_missing_walls),
	("time_weighted_rates_and_issues_union", test_time_weighted_rates_and_issues_union),
	("hit_with_cache_reset_turn", test_hit_with_cache_reset_turn),
	("hit_nan_without_cached", test_hit_nan_without_cached),
	("acc_sum_over_sum_not_mean_of_rates", test_acc_sum_over_sum_not_mean_of_rates),
	("acc_from_old_rows_weighted_by_tn", test_acc_from_old_rows_weighted_by_tn),
	("acc_none_without_draft", test_acc_none_without_draft),
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
