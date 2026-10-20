#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for llmtop_lib/log.py: stock llama.cpp logs (no timestamps, ANSI colours, router-forwarded lines, older wording).

	Standard library only. The main fixture is a real child log (b11669), already free of paths and names.

	python -I tools/win-llmtop/tests/test_log.py
"""

import math
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from llmtop_lib import log  # noqa: E402

FIXTURE = """\
0.00.195.785 I srv  llama_server: initializing ...
0.05.059.171 I srv  llama_server: model loaded
0.05.110.163 I slot get_availabl: id  0 | task -1 | selected slot by LRU, n_tokens = 0, t_last = -1
0.05.110.781 I slot launch_slot_: id  0 | task 0 | processing task, is_child = 0
0.10.858.458 I slot print_timing: id  0 | task 0 | prompt processing, n_tokens =   3662, progress = 0.88, t =   5.43 s / 673.81 tokens per second
0.12.542.516 I slot print_timing: id  0 | task 0 | prompt processing, n_tokens =   4174, progress = 1.00, t =   6.08 s / 686.15 tokens per second
0.14.307.579 I slot print_timing: id  0 | task 0 | prompt eval time =    7528.47 ms /  4178 tokens (    1.80 ms per token,   554.96 tokens per second)
0.14.307.584 I slot print_timing: id  0 | task 0 |        eval time =    1669.02 ms /   130 tokens (   12.94 ms per token,    77.29 tokens per second)
0.14.307.585 I slot print_timing: id  0 | task 0 |       total time =    9197.48 ms /  4308 tokens
0.14.307.585 I slot print_timing: id  0 | task 0 |    graphs reused =         22
0.14.307.586 I slot print_timing: id  0 | task 0 |   graph reallocs =          0    /     0        (same size) / (changed size)
0.14.307.590 I slot print_timing: id  0 | task 0 | draft acceptance = 0.93333 (   98 accepted /   105 generated), mean len =  4.50
0.14.307.750 I slot      release: id  0 | task 0 | stop processing: n_tokens = 4307, truncated = 0
0.15.726.340 I slot get_availabl: id  0 | task -1 | selected slot by LCP similarity, f_sim_best = 0.524 (> 0.100 thold), f_keep = 1.000
0.15.727.178 I slot launch_slot_: id  0 | task 73 | processing task, is_child = 0
0.22.201.226 I slot print_timing: id  0 | task 73 | prompt processing, n_tokens =   3909, progress = 1.00, t =   5.61 s / 696.49 tokens per second
0.23.475.481 I slot print_timing: id  0 | task 73 | prompt eval time =    6565.40 ms /  3913 tokens (    1.68 ms per token,   596.00 tokens per second)
0.23.475.486 I slot print_timing: id  0 | task 73 |        eval time =    1183.47 ms /    94 tokens (   12.73 ms per token,    78.58 tokens per second)
0.23.475.487 I slot print_timing: id  0 | task 73 |       total time =    7748.87 ms /  4007 tokens
0.23.475.500 I slot      release: id  0 | task 73 | stop processing: n_tokens = 4006, truncated = 0
"""
FIXTURE_LINES = FIXTURE.splitlines()
NO_TS = [re.sub(r"^\d+\.\d\d\.\d{3}\.\d{3} ", "", line) for line in FIXTURE_LINES]
COLORED = [line.replace(" I ", " \x1b[32mI \x1b[0m", 1) for line in FIXTURE_LINES]
FWD_59661 = ["[59661] " + line for line in FIXTURE_LINES]
FWD_8081 = ["[ 8081] " + line for line in FIXTURE_LINES]

OLDER = """\
0.00.100.000 I srv  llama_server: initializing ...
0.01.000.000 I srv  llama_server: model loaded
0.02.000.000 I slot get_availabl: id  0 | task -1 | selected slot by LCP similarity, sim_best = 0.850 (> 0.100 thold), f_keep = 1.000
0.02.001.000 I slot launch_slot_: id  0 | task 5 | processing task, is_child = 0
0.05.000.000 I slot print_timing: id  0 | task 5 | prompt eval time =    1000.00 ms /  1000 tokens (    1.00 ms per token,   1000.00 tokens per second)
0.06.000.000 I slot print_timing: id  0 | task 5 |        eval time =     500.00 ms /    50 tokens (   10.00 ms per token,     10.00 tokens per second)
0.06.001.000 I slot print_timing: id  0 | task 5 | draft acceptance rate = 0.60000 (    3 accepted /     5 generated)
0.07.000.000 I slot      release: id  0 | task 5 | stop processing: n_tokens = 1050
""".splitlines()

CACHE_HIT = """\
0.00.100.000 I srv  llama_server: initializing ...
0.01.000.000 I srv  llama_server: model loaded
0.02.001.000 I slot launch_slot_: id  0 | task 9 | processing task, is_child = 0
0.05.000.000 I slot print_timing: id  0 | task 9 | prompt eval time =       0.00 ms /     0 tokens (    0.00 ms per token,      inf tokens per second)
0.06.000.000 I slot print_timing: id  0 | task 9 |        eval time =     500.00 ms /    50 tokens (   10.00 ms per token,     10.00 tokens per second)
0.07.000.000 I slot      release: id  0 | task 9 | stop processing: n_tokens = 1050, truncated = 0
""".splitlines()

MISSING_EVAL = [line for line in CACHE_HIT if not re.search(r"\|\s*eval time =", line)]

COUNTERS = [
	"garbage line without a level letter",
	"0.00.100.000 I srv  llama_server: initializing ...",
	"more garbage",
	"0.01.000.000 I srv  llama_server: model loaded",
	"0.02.001.000 I slot launch_slot_: id  0 | task 3 | processing task, is_child = 0",
	"0.03.000.000 I slot      release: id  0 | task 3 | stop processing: n_tokens = 10, truncated = 0",
]


def feed(lines):
	asm = log.Assembler("preset")
	reqs, evs = [], []
	for line in lines:
		d, e = asm.feed(line)
		reqs += d
		evs += e
	return asm, reqs, evs


def check_fixture(reqs):
	"""The values the unchanged code produced on the fixture: the change must not alter them."""
	assert len(reqs) == 2, "expected two requests, got %d" % len(reqs)
	r0, r73 = reqs
	assert r0["task"] == 0 and r73["task"] == 73
	assert (r0["pn"], r0["tn"], r0["pp"], r0["tg"]) == (4178, 130, 554.96, 77.29)
	assert r0["acc"] == 0.93333 and r0["mlen"] == 4.50
	assert r0["ms"] == 9197.48 and r0["kv"] == 4307 and r0["cached"] == 0
	assert r0["sel"] == "LRU" and r0["sim"] == 0.0
	assert r73["pn"] == 3913 and r73["tn"] == 94 and r73["pp"] == 596.00 and r73["tg"] == 78.58
	assert r73["sel"] == "LCP" and r73["sim"] == 0.524
	assert r73["ms"] == 7748.87 and r73["kv"] == 4006 and r73["cached"] == 0
	assert "acc" not in r73


def test_b11669_with_timestamps():
	asm, reqs, _ = feed(FIXTURE_LINES)
	check_fixture(reqs)
	assert asm.timestamps is True
	assert reqs[0]["t0"] == 5.110781 and reqs[0]["t"] == 14.307750
	assert reqs[1]["t0"] == 15.727178 and reqs[1]["t"] == 23.475500
	assert len(asm.sessions) == 1 and asm.sessions[0]["load_s"] == 5.059171


def test_no_timestamps():
	asm, reqs, _ = feed(NO_TS)
	check_fixture(reqs)
	assert asm.timestamps is False


def test_colours():
	asm, reqs, _ = feed(COLORED)
	check_fixture(reqs)
	assert asm.timestamps is True


def test_router_forwarded():
	asm, reqs, _ = feed(FWD_59661)
	check_fixture(reqs)
	assert asm.last_port == 59661
	asm, reqs, _ = feed(FWD_8081)
	check_fixture(reqs)
	assert asm.last_port == 8081


def test_older_wording():
	_, reqs, _ = feed(OLDER)
	assert len(reqs) == 1
	r = reqs[0]
	assert r["sel"] == "LCP" and r["sim"] == 0.85
	assert r["acc"] == 0.6 and r["mlen"] is None
	assert r["pn"] == 1000 and r["tn"] == 50 and r["kv"] == 1050
	assert r["trunc"] is False and r["cached"] == 0


def test_full_cache_hit():
	_, reqs, _ = feed(CACHE_HIT)
	assert len(reqs) == 1
	r = reqs[0]
	assert r["pn"] == 0 and math.isnan(r["pp"])  # the inf rate is recorded as NaN
	assert r["tn"] == 50 and r["kv"] == 1050
	assert r["cached"] == 1000  # kv - tn - pn: the whole prompt, not inflated


def test_missing_eval_line():
	_, reqs, _ = feed(MISSING_EVAL)
	assert len(reqs) == 1
	assert reqs[0]["cached"] is None  # tn was never parsed


def test_counters():
	asm, reqs, _ = feed(COUNTERS)
	assert asm.lines_seen == len(COUNTERS)
	assert asm.lines_parsed == len(COUNTERS) - 2  # the two garbage lines match no level letter
	assert asm.tasks_seen == 1
	assert asm.requests_done == 1
	assert len(reqs) == 1


def test_parse_router_forwarded_no_timestamps():
	lines = [
		"0.00.100.000 I srv  router: starting",
		"[59661] 0.01.000.000 I srv  join: request for name=m1 queued at position 1",
		"[59661] I srv  tick: evicting idle LRU name=m1",
	]
	evs = log.parse_router(lines, router_start=1000.0)
	assert [e["kind"] for e in evs] == ["queue", "unload"]
	assert evs[0]["t"] == 1.0 and evs[0]["wall"] == 1001.0 and evs[0]["port"] == 59661
	assert evs[1]["t"] == 1.0 and evs[1]["wall"] == 1001.0 and evs[1]["port"] == 59661  # no timestamp: the last known t


def test_router_log_start_and_router_logs():
	with tempfile.TemporaryDirectory() as td:
		dated = os.path.join(td, "llama-server-20261020-103619.log")
		other = os.path.join(td, "router.log")
		open(dated, "w").close()
		open(other, "w").close()
		start = time.mktime(time.strptime("20261020103619", "%Y%m%d%H%M%S"))
		assert log.router_log_start(dated) == start
		assert log.router_log_start(other) is None
		assert log.router_logs(td) == [(start, dated)]
		assert log.router_logs(None, [other]) == [(None, other)]
		assert log.router_logs(td, [other]) == [(start, dated), (None, other)]
		assert log.session_starts(None) == {}


CASES = [
	("b11669_with_timestamps", test_b11669_with_timestamps),
	("no_timestamps", test_no_timestamps),
	("colours", test_colours),
	("router_forwarded", test_router_forwarded),
	("older_wording", test_older_wording),
	("full_cache_hit", test_full_cache_hit),
	("missing_eval_line", test_missing_eval_line),
	("counters", test_counters),
	("parse_router_forwarded_no_timestamps", test_parse_router_forwarded_no_timestamps),
	("router_log_start_and_router_logs", test_router_log_start_and_router_logs),
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
