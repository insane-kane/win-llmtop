#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Aaron Kane <software@kanenet.us>
"""Unit and live tests for llmtop_lib.win.

	python -I tools/win-llmtop/tests/test_win.py
"""

import ctypes
import math
import os
import pathlib
import sys
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llmtop_lib import win  # noqa: E402


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


class _StubAdl:
	"""A stand-in for atiadlxx.dll: `names` are the adapters it lists, in order; only the indexes in `answers` have a live sensor, and each answer is
	(activity %, graphics clock MHz). A non-zero `rc` fails every sensor query, like a driver that does not answer."""

	def __init__(self, names, answers, rc=0):
		self.names, self.answers, self.rc, self.queried = names, answers, rc, []

	def ADL2_Main_Control_Create(self, malloc, enum, ctx):
		return 0

	def ADL2_Adapter_NumberOfAdapters_Get(self, ctx, n):
		n._obj.value = len(self.names)
		return 0

	def ADL2_Adapter_AdapterInfo_Get(self, ctx, info, size):
		for i, name in enumerate(self.names):
			info[i].index = i
			info[i].name = name.encode("utf-8")
		return 0

	def ADL2_New_QueryPMLogData_Get(self, ctx, index, log):
		self.queried.append(index)
		if self.rc or index not in self.answers:
			return self.rc or -1
		load, mhz = self.answers[index]
		pm = log._obj
		pm.sensors[win.ADL_SENSOR_ACTIVITY_GFX].supported = 1
		pm.sensors[win.ADL_SENSOR_ACTIVITY_GFX].value = load
		pm.sensors[win.ADL_SENSOR_GFXCLK].supported = 1
		pm.sensors[win.ADL_SENSOR_GFXCLK].value = mhz
		return 0


class TestWin(unittest.TestCase):
	def test_counters_sample_per_luid_and_engine_spaces(self):
		igpu = "0x00000000_0x0000A1B2"
		dgpu = "0x00000000_0x0001C3D4"
		data = {
			"p_ded": {
				"pid_4200_luid_0x00000000_0x0000a1b2_phys_0": 128 * win.MIB,
				"pid_4200_luid_0x00000000_0x0001c3d4_phys_0": 2048 * win.MIB,
			},
			"p_shr": {
				"pid_4200_luid_0x00000000_0x0000a1b2_phys_0": 64 * win.MIB,
				"pid_4200_luid_0x00000000_0x0001c3d4_phys_0": 256 * win.MIB,
			},
			"p_com": {
				"pid_4200_luid_0x00000000_0x0000a1b2_phys_0": 192 * win.MIB,
				"pid_4200_luid_0x00000000_0x0001c3d4_phys_0": 2304 * win.MIB,
			},
			"a_ded": {
				"luid_0x00000000_0x0000a1b2_phys_0": 200 * win.MIB,
				"luid_0x00000000_0x0001c3d4_phys_0": 4096 * win.MIB,
			},
			"a_shr": {
				"luid_0x00000000_0x0000a1b2_phys_0": 100 * win.MIB,
				"luid_0x00000000_0x0001c3d4_phys_0": 512 * win.MIB,
			},
			"a_com": {
				"luid_0x00000000_0x0000a1b2_phys_0": 300 * win.MIB,
				"luid_0x00000000_0x0001c3d4_phys_0": 4608 * win.MIB,
			},
			"eng": {
				"pid_4200_luid_0x00000000_0x0000a1b2_phys_0_eng_0_engtype_Video Decode": 12.5,
				"pid_4200_luid_0x00000000_0x0000a1b2_phys_0_eng_1_engtype_Video Processing": 7.5,
				"pid_4200_luid_0x00000000_0x0001c3d4_phys_0_eng_0_engtype_3D": 60.0,
				"pid_4200_luid_0x00000000_0x0001c3d4_phys_0_eng_1_engtype_3D": 55.0,
				"pid_4200_luid_0x00000000_0x0001c3d4_phys_0_eng_2_engtype_High Priority Compute": 33.0,
			},
			"pages_in": {"_": 2.0},
			"pages_out": {"_": 1.0},
			"standby": {"_": 512 * win.MIB},
			"standby_core": {"_": 128 * win.MIB},
			"standby_res": {"_": 64 * win.MIB},
			"free": {"_": 1024 * win.MIB},
			"modified": {"_": 32 * win.MIB},
			"pagefile": {"_": 4.5},
		}
		c = object.__new__(win.Counters)
		c.q = _StubQuery(data)
		c.next = None
		c.eng_hist = {}
		out = c.sample()

		p = out["procs"][4200]
		self.assertAlmostEqual(p["ded"], 2176.0)
		self.assertAlmostEqual(p["shr"], 320.0)
		self.assertAlmostEqual(p["com"], 2496.0)
		self.assertEqual(p["eng"], {
			"Video Decode": 12.5,
			"Video Processing": 7.5,
			"3D": 100.0,
			"High Priority Compute": 33.0,
		})

		by_luid = out["procs_by_luid"][4200]
		self.assertEqual(set(by_luid.keys()), {igpu, dgpu})
		self.assertAlmostEqual(by_luid[igpu]["ded"], 128.0)
		self.assertAlmostEqual(by_luid[igpu]["shr"], 64.0)
		self.assertAlmostEqual(by_luid[igpu]["com"], 192.0)
		self.assertEqual(by_luid[igpu]["eng"], {"Video Decode": 12.5, "Video Processing": 7.5})
		self.assertAlmostEqual(by_luid[dgpu]["ded"], 2048.0)
		self.assertAlmostEqual(by_luid[dgpu]["shr"], 256.0)
		self.assertAlmostEqual(by_luid[dgpu]["com"], 2304.0)
		self.assertEqual(by_luid[dgpu]["eng"], {"3D": 100.0, "High Priority Compute": 33.0})

	def test_counters_engine_load_is_a_mean_over_three_seconds(self):
		# one compute instance reads 100 or 0 from one 250 ms sample to the next, as Windows' counters do on AMD during compute work
		name = "pid_4200_luid_0x00000000_0x0001c3d4_phys_0_eng_0_engtype_Compute"
		c = object.__new__(win.Counters)
		c.q = _StubQuery({"eng": {name: 100.0}})
		c.next = None
		c.eng_hist = {}
		clock, seen = [0.0], []
		with mock.patch.object(win.time, "monotonic", side_effect=lambda: clock[0]):
			for i in range(12):
				c.q.data["eng"][name] = 100.0 if i % 2 == 0 else 0.0
				out = c.sample()
				seen.append(out["procs"].get(4200, {}).get("eng", {}).get("Compute"))
				clock[0] += 0.25
		means = [v for v in seen[2:] if v is not None]
		self.assertEqual(len(means), 10, seen)
		for v in means:
			self.assertGreater(v, 25.0)
			self.assertLess(v, 75.0)
			self.assertNotIn(v, (0.0, 100.0))

	def test_counters_engine_history_drops_instances_gone_for_longer_than_the_window(self):
		name = "pid_4200_luid_0x00000000_0x0001c3d4_phys_0_eng_0_engtype_Compute"
		c = object.__new__(win.Counters)
		c.q = _StubQuery({"eng": {name: 50.0}})
		c.next = None
		c.eng_hist = {}
		clock = [0.0]
		with mock.patch.object(win.time, "monotonic", side_effect=lambda: clock[0]):
			c.sample()
			self.assertIn(name, c.eng_hist)
			c.q.data["eng"] = {}  # the instance is not in this sample
			clock[0] = win.ENG_WINDOW_S - 0.5
			c.sample()
			self.assertIn(name, c.eng_hist)  # still inside the window
			clock[0] = win.ENG_WINDOW_S + 0.5
			c.sample()
			self.assertNotIn(name, c.eng_hist)

	def test_amd_load_reads_the_driver_sensor(self):
		name = "AMD Radeon RX 7900 XT"
		# one card is listed once per output: only the second listing has live sensors, and the name match ignores case
		dll = _StubAdl([name, name], {1: (98, 2876)})
		amd = win.AmdLoad("amd radeon rx 7900 xt", dll=dll)
		self.assertEqual(amd.index, 1)
		self.assertEqual(amd.read(), {"load": 98.0, "mhz": 2876.0})
		dll.rc = -5  # the query fails: no reading rather than a wrong one
		self.assertIsNone(amd.read())
		clamped = win.AmdLoad(name, dll=_StubAdl([name], {0: (130, 2000)}))
		self.assertEqual(clamped.read(), {"load": 100.0, "mhz": 2000.0})
		other = win.AmdLoad("AMD Radeon PRO W6800", dll=_StubAdl([name], {0: (98, 2876)}))
		self.assertIsNone(other.index)
		self.assertIsNone(other.read())

	def test_pmc_ex2_to_ex_fallback(self):
		real_fn = win._psapi.GetProcessMemoryInfo
		saved_cb = win._pmc_cb
		calls = []

		def stub_get_pmc(h, pm_ptr, cb):
			calls.append(cb)
			if cb == 96:
				return 0
			pm = ctypes.cast(pm_ptr, ctypes.POINTER(win._Pmc)).contents
			pm.ws = 120 * win.MIB
			pm.private = 80 * win.MIB
			pm.faults = 42
			return 1

		try:
			win._pmc_cb = win._PMC_EX2_SIZE
			win._psapi.GetProcessMemoryInfo = stub_get_pmc
			pid = os.getpid()
			pm1 = win.proc_mem(pid)
			self.assertIsNotNone(pm1)
			self.assertEqual(calls, [96, 80])
			self.assertEqual(win._pmc_cb, 80)
			self.assertAlmostEqual(pm1["ws"], 120.0)
			self.assertAlmostEqual(pm1["commit"], 80.0)
			self.assertEqual(pm1["faults"], 42)
			self.assertTrue(math.isnan(pm1["pws"]))

			calls.clear()
			pm2 = win.proc_mem(pid)
			self.assertIsNotNone(pm2)
			self.assertEqual(calls, [80])

			calls.clear()
			ws_by, ws_total = win.working_sets({pid: ("python.exe", 0)})
			self.assertEqual(calls, [80])
			self.assertAlmostEqual(ws_total, 120.0)
			self.assertEqual(ws_by["python.exe"][1], 1)
		finally:
			win._psapi.GetProcessMemoryInfo = real_fn
			win._pmc_cb = saved_cb

	def test_live_this_process_and_system(self):
		pid = os.getpid()
		now = time.time()
		info = win.proc_info(pid)
		self.assertIsNotNone(info["exe"])
		self.assertEqual(os.path.normcase(info["exe"]), os.path.normcase(sys.executable))
		self.assertIsInstance(info["argv"], list)
		self.assertTrue(any("test_win.py" in a for a in info["argv"]), info["argv"])
		self.assertIsInstance(info["created"], float)
		self.assertLessEqual(abs(now - info["created"]), 600.0)
		self.assertNotIn(("proc_info", pid), win.errors)

		pm = win.proc_mem(pid)
		self.assertIsInstance(pm, dict)
		self.assertGreater(pm["ws"], 0.0)
		self.assertNotIn(("proc_mem", pid), win.errors)

		vm = win.vm_summary(pid)
		self.assertIsInstance(vm, dict)
		self.assertGreater(vm["private"], 0.0)
		self.assertIn("files", vm)
		self.assertIn("big", vm)
		self.assertNotIn(("vm_summary", pid), win.errors)

		procs = win.processes()
		self.assertIn(pid, procs)

		sm = win.sys_mem()
		self.assertIsInstance(sm, dict)
		self.assertGreater(sm["total"], 0.0)

		pfs = win.pagefiles()
		self.assertIsInstance(pfs, list)

		dx = win.Dxgi()
		self.assertIsInstance(dx.adapters, list)
		for a in dx.adapters:
			self.assertIn("shared", a)
			self.assertGreaterEqual(a["shared"], 0.0)

		self.assertIsInstance(win.enable_vt(), bool)

	def test_proc_info_nonexistent_pid(self):
		bad_pid = 0xFFFFFFFC
		win.errors.pop(("proc_info", bad_pid), None)
		info = win.proc_info(bad_pid)
		self.assertEqual(info, {"exe": None, "argv": None, "created": None})
		self.assertIn(("proc_info", bad_pid), win.errors)
		code, msg = win.errors[("proc_info", bad_pid)]
		self.assertIsInstance(code, int)
		self.assertNotEqual(code, 0)
		self.assertTrue(bool(msg))

	def test_pagefiles_none_returns_empty(self):
		real_fn = win._ntdll.NtQuerySystemInformation

		def stub(info_class, buf, length, ret_len):
			return 0  # success with nothing written; ret_len stays 0

		try:
			win._ntdll.NtQuerySystemInformation = stub
			self.assertEqual(win.pagefiles(), [])
		finally:
			win._ntdll.NtQuerySystemInformation = real_fn

	def test_vm_summary_needs_query_information_only(self):
		real_fn = win._k32.OpenProcess
		masks = []

		def wrapper(access, inherit, pid):
			masks.append(access)
			return real_fn(access, inherit, pid)

		try:
			win._k32.OpenProcess = wrapper
			vm = win.vm_summary(os.getpid(), name_mib=0)
		finally:
			win._k32.OpenProcess = real_fn
		self.assertEqual(masks, [win.PROCESS_QUERY_INFORMATION])
		self.assertIsNotNone(vm)
		self.assertGreater(vm["private"], 0.0)
		self.assertTrue(vm["files"])


CASES = [
	("counters_sample_per_luid_and_engine_spaces", TestWin("test_counters_sample_per_luid_and_engine_spaces")),
	("counters_engine_load_is_a_mean_over_three_seconds", TestWin("test_counters_engine_load_is_a_mean_over_three_seconds")),
	("counters_engine_history_drops_instances_gone_for_longer_than_the_window", TestWin("test_counters_engine_history_drops_instances_gone_for_longer_than_the_window")),
	("amd_load_reads_the_driver_sensor", TestWin("test_amd_load_reads_the_driver_sensor")),
	("pmc_ex2_to_ex_fallback", TestWin("test_pmc_ex2_to_ex_fallback")),
	("live_this_process_and_system", TestWin("test_live_this_process_and_system")),
	("proc_info_nonexistent_pid", TestWin("test_proc_info_nonexistent_pid")),
	("pagefiles_none_returns_empty", TestWin("test_pagefiles_none_returns_empty")),
	("vm_summary_needs_query_information_only", TestWin("test_vm_summary_needs_query_information_only")),
]


def main():
	failed = 0
	for name, case in CASES:
		try:
			getattr(case, case._testMethodName)()
		except Exception as e:
			failed += 1
			print("FAIL %s: %s: %s" % (name, type(e).__name__, e))
		else:
			print("PASS %s" % name)
	if failed:
		print("%d of %d cases failed" % (failed, len(CASES)))
		sys.exit(1)
	print("ALL PASS")


if __name__ == "__main__":
	main()
