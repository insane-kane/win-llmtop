#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
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
