#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Aaron Kane <software@kanenet.us>
"""Unit tests for CPU telemetry in win.py, state.py and pages.py.

	Standard library only. Tests per-core instance ordering/filtering, hybrid and non-hybrid
	P/E core derivation, the pure cache-grouping helper, Live cpu_block with NaN parts omitted,
	and the System CPU section with topology and with error set.

	python -I tools/win-llmtop/tests/test_cpu.py
"""

import math
import os
import re
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from llmtop_lib import pages, state as S, win  # noqa: E402

NAN = float("nan")


class _StubQuery:
	def __init__(self, data):
		self.data = data
		self.collected = 1

	def collect(self):
		self.collected += 1

	def read(self, key):
		return self.data.get(key, {})

	def close(self):
		pass


class _Ring:
	def cols(self, *args):
		return []

	def span_max(self, a, b):
		return NAN


class _Rings:
	def __getitem__(self, key):
		return _Ring()


def _plain(lines):
	return [re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", ln) for ln in lines]


def _collector(cpu_info=None):
	return types.SimpleNamespace(
		requests=[],
		follow={},
		flags={},
		events=[],
		cfg=dict(S.CFG),
		rings=_Rings(),
		allow=0.0,
		allow_known=False,
		cpu_info=cpu_info,
	)


def test_cpu_core_ordering_and_filtering():
	raw = {
		"_Total": 42.0,
		"0,_Total": 40.0,
		"1,_Total": 44.0,
		"0,10": 10.5,
		"0,2": 2.5,
		"0,0": 0.5,
		"1,0": 50.0,
		"1,2": 52.0,
	}
	assert win._cpu_cores(raw) == [0.5, 2.5, 10.5, 50.0, 52.0]
	assert win._cpu_cores({}) == []

	c = object.__new__(win.Counters)
	c.q = _StubQuery({
		"cpu_util": {"_Total": 112.5},
		"cpu_core": raw,
		"cpu_mhz_base": {"_Total": 3600.0},
		"cpu_perf": {"_Total": 125.0},
		"cpu_queue": {"": 3.0},
	})
	c.next = None
	sy = c.sample()["sys"]
	assert sy["cpu"] == 112.5, sy
	assert sy["cpu_mhz"] == 4500.0, sy
	assert sy["cpu_queue"] == 3.0, sy
	assert sy["cpu_cores"] == [0.5, 2.5, 10.5, 50.0, 52.0], sy

	c.q = _StubQuery({})
	sy_empty = c.sample()["sys"]
	assert math.isnan(sy_empty["cpu"]), sy_empty
	assert math.isnan(sy_empty["cpu_mhz"]), sy_empty
	assert math.isnan(sy_empty["cpu_queue"]), sy_empty
	assert sy_empty["cpu_cores"] == [], sy_empty


def test_pe_split_hybrid_and_non_hybrid():
	non_hybrid = [{"efficiency_class": 0, "threads": 2} for _ in range(12)]
	assert win._split_cores(non_hybrid) == (12, 24, 12, 0)

	hybrid = (
		[{"efficiency_class": 1, "threads": 2} for _ in range(8)]
		+ [{"efficiency_class": 0, "threads": 1} for _ in range(16)]
	)
	assert win._split_cores(hybrid) == (24, 32, 8, 16)
	assert win._split_cores([]) == (0, 0, 0, 0)


def test_cache_grouping():
	records = []
	for _ in range(12):
		records.append({"level": 2, "type": 0, "size": 1280 * 1024, "line": 64, "shared": 2})
		records.append({"level": 1, "type": 1, "size": 32 * 1024, "line": 64, "shared": 2})
		records.append({"level": 1, "type": 2, "size": 48 * 1024, "line": 64, "shared": 2})
	records.append({"level": 3, "type": 0, "size": 36 * 1024 * 1024, "line": 64, "shared": 24})

	grouped = win._group_caches(records)
	assert grouped == [
		{"level": 1, "type": "d", "size": 49152, "line": 64, "count": 12, "shared": 2},
		{"level": 1, "type": "i", "size": 32768, "line": 64, "count": 12, "shared": 2},
		{"level": 2, "type": "u", "size": 1310720, "line": 64, "count": 12, "shared": 2},
		{"level": 3, "type": "u", "size": 37748736, "line": 64, "count": 1, "shared": 24},
	], grouped


def test_live_cpu_block_nan_omitted():
	c = _collector()
	view = pages.View()

	snap_full = {
		"t": 1000.0,
		"cpu": {"cpu": 23.4, "cpu_mhz": 3900.0, "cpu_queue": 0.0, "cpu_cores": []},
		"llama": {"present": True, "cpu": 1.4},
	}
	text = "\n".join(_plain(pages.cpu_block(c, snap_full, view, 120, 2)))
	assert "CPU" in text and "23%" in text, text
	assert "llama-server 1.4 cores · 3.9 GHz · queue 0" in text, text

	snap_turbo = {
		"t": 1000.0,
		"cpu": {"cpu": 118.0, "cpu_mhz": NAN, "cpu_queue": 2.0, "cpu_cores": []},
		"llama": {"present": True, "cpu": 1.4},
	}
	text = "\n".join(_plain(pages.cpu_block(c, snap_turbo, view, 120, 2)))
	assert "100%" in text, text
	assert "llama-server 1.4 cores · queue 2" in text, text
	assert "GHz" not in text, text

	snap_no_llama = {
		"t": 1000.0,
		"cpu": {"cpu": 15.0, "cpu_mhz": 4100.0, "cpu_queue": NAN, "cpu_cores": []},
		"llama": {"present": False, "cpu": NAN},
	}
	text = "\n".join(_plain(pages.cpu_block(c, snap_no_llama, view, 120, 2)))
	assert "4.1 GHz" in text, text
	assert "llama-server" not in text and "queue" not in text, text


def test_system_cpu_section_and_error():
	info = {
		"name": "Generic 24-Core Processor",
		"cores": 24,
		"threads": 32,
		"p_cores": 8,
		"e_cores": 16,
		"caches": [
			{"level": 1, "type": "d", "size": 48 * 1024, "line": 64, "count": 12, "shared": 2},
			{"level": 1, "type": "i", "size": 32 * 1024, "line": 64, "count": 12, "shared": 2},
			{"level": 2, "type": "u", "size": 2 * 1024 * 1024, "line": 64, "count": 12, "shared": 2},
			{"level": 3, "type": "u", "size": 36 * 1024 * 1024, "line": 64, "count": 1, "shared": 24},
		],
		"error": None,
	}
	snap = {
		"t": 1000.0,
		"cpu": {"cpu": 35.0, "cpu_mhz": 4200.0, "cpu_queue": 1.0, "cpu_cores": [10.0, 60.0, 90.0, 100.0]},
		"llama": {"present": True, "cpu": 2.5, "ws": 512.0, "pws": 256.0, "commit": 512.0, "pids": [100], "vm": None},
		"ram": {
			"total": 32768.0, "avail": 16384.0, "commit": 8192.0, "commit_limit": 32768.0, "commit_max": 32768.0,
			"standby": 4096.0, "free": 12288.0, "modified": 128.0, "pages_in": 0.0, "pages_out": 0.0,
			"pagefile_pct": 0.0, "pagefiles": [], "pf_cfg": [], "ws_by": {}, "ws_total": 4096.0,
		},
	}
	raw_lines = pages.system(_collector(info), snap, pages.View(), 120, 80)
	lines = _plain(raw_lines)
	text = "\n".join(lines)
	cpu_idx = next(i for i, ln in enumerate(lines) if "CPU" in ln and "Generic 24-Core Processor" in ln)
	ram_idx = next(i for i, ln in enumerate(lines) if "SYSTEM RAM" in ln)
	assert cpu_idx < ram_idx, (cpu_idx, ram_idx)
	assert "24 cores (8P + 16E), 32 threads" in lines[cpu_idx], lines[cpu_idx]
	assert "L1d 48 KiB × 12 (2 threads each)" in text, text
	assert "L3 36 MiB × 1 (24 threads each)" in text, text
	assert "CPU in use" in text and "llama-server" in text and "clock" in text, text
	assert any(pages.C.bgreen in ln and pages.C.byellow in ln and pages.C.bred in ln for ln in raw_lines), raw_lines

	err_info = {"name": "", "cores": 0, "threads": 0, "p_cores": 0, "e_cores": 0, "caches": [], "error": "topology call failed"}
	err_lines = _plain(pages.system(_collector(err_info), snap, pages.View(), 120, 80))
	err_text = "\n".join(err_lines)
	assert "topology call failed" in err_text, err_text
	assert "L1d" not in err_text, err_text
	assert "CPU in use" in err_text and "llama-server" in err_text and "clock" in err_text, err_text


CASES = [
	("cpu_core_ordering_and_filtering", test_cpu_core_ordering_and_filtering),
	("pe_split_hybrid_and_non_hybrid", test_pe_split_hybrid_and_non_hybrid),
	("cache_grouping", test_cache_grouping),
	("live_cpu_block_nan_omitted", test_live_cpu_block_nan_omitted),
	("system_cpu_section_and_error", test_system_cpu_section_and_error),
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
