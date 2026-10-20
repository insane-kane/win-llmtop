# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows telemetry for llmtop. ctypes only, no third-party packages.

	PDH     per-process dedicated / shared / committed VRAM, GPU engine load, paging rates (a few ms per sample)
	DXGI    the VRAM budget the OS reports for the calling process (it can be below the card's capacity)
	psapi   per-process working set, private bytes, CPU time; system RAM and commit charge
"""

import ctypes
import ctypes.wintypes as wt
import os
import re
import uuid

MIB = 2**20
WIN = os.name == "nt"
IS_64 = ctypes.sizeof(ctypes.c_void_p) == 8
PDH_FMT_DOUBLE = 0x200
PDH_MORE_DATA = 0x800007D2
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_COMMAND_LINE_INFORMATION = 60
STD_OUTPUT_HANDLE = wt.DWORD(-11).value
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
EPOCH_AS_FILETIME = 116444736000000000

errors = {}

PATHS = {
	"p_ded": r"\GPU Process Memory(*)\Dedicated Usage",
	"p_shr": r"\GPU Process Memory(*)\Shared Usage",
	"p_com": r"\GPU Process Memory(*)\Total Committed",
	"a_ded": r"\GPU Adapter Memory(*)\Dedicated Usage",
	"a_shr": r"\GPU Adapter Memory(*)\Shared Usage",
	"a_com": r"\GPU Adapter Memory(*)\Total Committed",
	"eng": r"\GPU Engine(*)\Utilization Percentage",
	"pages_in": r"\Memory\Pages Input/sec",
	"pages_out": r"\Memory\Pages Output/sec",
	"standby": r"\Memory\Standby Cache Normal Priority Bytes",
	"standby_core": r"\Memory\Standby Cache Core Bytes",
	"standby_res": r"\Memory\Standby Cache Reserve Bytes",
	"free": r"\Memory\Free & Zero Page List Bytes",
	"modified": r"\Memory\Modified Page List Bytes",
	"pagefile": r"\Paging File(_Total)\% Usage",
}
RE_PID = re.compile(r"pid_(\d+)_luid_0x([0-9A-Fa-f]+)_0x([0-9A-Fa-f]+)", re.I)
RE_LUID = re.compile(r"luid_0x([0-9A-Fa-f]+)_0x([0-9A-Fa-f]+)", re.I)
RE_ENG = re.compile(r"pid_(\d+)_luid_0x([0-9A-Fa-f]+)_0x([0-9A-Fa-f]+).*engtype_(.+)$", re.I)


def _fmt_luid(hi, lo):
	return "0x%08X_0x%08X" % (int(hi, 16), int(lo, 16))


class _Val(ctypes.Structure):
	_fields_ = [("status", wt.DWORD), ("value", ctypes.c_double)]


class _Item(ctypes.Structure):
	_fields_ = [("name", ctypes.c_wchar_p), ("val", _Val)]


class _Query:
	"""One PDH query with wildcard counters; the instance list is fixed when the query is built."""

	def __init__(self, paths):
		self.pdh = ctypes.WinDLL("pdh", use_last_error=True)
		self.pdh.PdhOpenQueryW.argtypes = [wt.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
		self.pdh.PdhOpenQueryW.restype = wt.LONG
		self.pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
		self.pdh.PdhAddEnglishCounterW.restype = wt.LONG
		self.pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
		self.pdh.PdhCollectQueryData.restype = wt.LONG
		self.pdh.PdhGetFormattedCounterArrayW.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
		self.pdh.PdhGetFormattedCounterArrayW.restype = wt.LONG
		self.pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
		self.pdh.PdhCloseQuery.restype = wt.LONG
		self.h = ctypes.c_void_p()
		if self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.h)):
			raise OSError("PdhOpenQuery failed")
		self.counters = {}
		for key, path in paths.items():
			c = ctypes.c_void_p()
			if self.pdh.PdhAddEnglishCounterW(self.h, path, 0, ctypes.byref(c)) == 0:
				self.counters[key] = c
		self.collected = 0

	def collect(self):
		self.pdh.PdhCollectQueryData(self.h)
		self.collected += 1

	def read(self, key):
		c = self.counters.get(key)
		if c is None:
			return {}
		size, n = wt.DWORD(0), wt.DWORD(0)
		r = self.pdh.PdhGetFormattedCounterArrayW(c, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(n), None)
		if (r & 0xFFFFFFFF) != PDH_MORE_DATA:
			return {}
		buf = ctypes.create_string_buffer(size.value)
		if self.pdh.PdhGetFormattedCounterArrayW(c, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(n), buf):
			return {}
		items = ctypes.cast(buf, ctypes.POINTER(_Item))
		return {items[i].name: items[i].val.value for i in range(n.value) if items[i].val.status in (0, 1)}

	def close(self):
		self.pdh.PdhCloseQuery(self.h)


class Counters:
	"""GPU and memory counters. Rebuilds its query on request: processes started later only appear in a fresh query."""

	def __init__(self):
		self.q = _Query(PATHS)
		self.q.collect()
		self.next = None

	def rebuild(self):
		if self.next is None:
			self.next = _Query(PATHS)
			self.next.collect()

	def sample(self):
		"""{'procs': {pid: {ded, shr, com (MiB), eng: {type: %}}}, 'procs_by_luid': {pid: {luid: {ded, shr, com, eng}}}, 'adapters': {luid: {ded, shr, com}}, 'sys': {...}}"""
		if self.next is not None and self.next.collected >= 2:
			self.q.close()
			self.q, self.next = self.next, None
		self.q.collect()
		if self.next is not None:
			self.next.collect()
		procs, procs_by_luid, adapters = {}, {}, {}
		new = lambda: {"ded": 0.0, "shr": 0.0, "com": 0.0, "eng": {}}
		for key, field in (("p_ded", "ded"), ("p_shr", "shr"), ("p_com", "com")):
			for name, v in self.q.read(key).items():
				m = RE_PID.search(name)
				if m:
					pid, luid = int(m.group(1)), _fmt_luid(m.group(2), m.group(3))
					procs.setdefault(pid, new())[field] += v / MIB
					procs_by_luid.setdefault(pid, {}).setdefault(luid, new())[field] += v / MIB
		for key, field in (("a_ded", "ded"), ("a_shr", "shr"), ("a_com", "com")):
			for name, v in self.q.read(key).items():
				m = RE_LUID.search(name)
				if m:
					adapters.setdefault(_fmt_luid(m.group(1), m.group(2)), new())[field] += v / MIB
		for name, v in self.q.read("eng").items():
			m = RE_ENG.search(name)
			if m and v > 0:
				pid, luid, eng = int(m.group(1)), _fmt_luid(m.group(2), m.group(3)), m.group(4)
				e = procs.setdefault(pid, new())["eng"]
				e[eng] = min(100.0, e.get(eng, 0.0) + v)
				el = procs_by_luid.setdefault(pid, {}).setdefault(luid, new())["eng"]
				el[eng] = min(100.0, el.get(eng, 0.0) + v)
		one = lambda k: next(iter(self.q.read(k).values()), 0.0)
		return {"procs": procs, "procs_by_luid": procs_by_luid, "adapters": adapters, "sys": {
			"pages_in": one("pages_in"), "pages_out": one("pages_out"), "standby": (one("standby") + one("standby_core") + one("standby_res")) / MIB, "free": one("free") / MIB,
			"modified": one("modified") / MIB, "pagefile_pct": one("pagefile")}}


# ---- DXGI: the VRAM budget ---------------------------------------------------------------------------------------

class _Guid(ctypes.Structure):
	_fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]


class _Luid(ctypes.Structure):
	_fields_ = [("low", wt.DWORD), ("high", wt.LONG)]


class _Desc1(ctypes.Structure):
	_fields_ = [("name", ctypes.c_wchar * 128), ("vendor", wt.UINT), ("device", wt.UINT), ("subsys", wt.UINT), ("rev", wt.UINT),
		("vram", ctypes.c_size_t), ("sysmem", ctypes.c_size_t), ("shared", ctypes.c_size_t), ("luid", _Luid), ("flags", wt.UINT)]


class _MemInfo(ctypes.Structure):
	_fields_ = [("budget", ctypes.c_uint64), ("usage", ctypes.c_uint64), ("avail_reserve", ctypes.c_uint64), ("reservation", ctypes.c_uint64)]


def _guid(s):
	u, g = uuid.UUID(s), _Guid()
	g.a, g.b, g.c = u.time_low, u.time_mid, u.time_hi_version
	for i, byte in enumerate(u.bytes[8:]):
		g.d[i] = byte
	return g


def _vcall(obj, idx, *argtypes):
	vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
	return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[idx])


class Dxgi:
	"""Hardware adapters with their VRAM capacity and the budget the OS currently grants a process."""

	def __init__(self):
		self.adapters = []
		dxgi = ctypes.WinDLL("dxgi", use_last_error=True)
		dxgi.CreateDXGIFactory1.argtypes = [ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p)]
		dxgi.CreateDXGIFactory1.restype = ctypes.c_long
		factory = ctypes.c_void_p()
		iid_factory, iid_adapter3 = _guid("770aae78-f26f-4dba-a829-253c83d1b387"), _guid("645967a4-1392-4310-a798-8053ce3e93fd")
		if dxgi.CreateDXGIFactory1(ctypes.byref(iid_factory), ctypes.byref(factory)):
			return
		self._factory = factory
		i = 0
		while True:
			ad = ctypes.c_void_p()
			if _vcall(factory, 12, wt.UINT, ctypes.POINTER(ctypes.c_void_p))(factory, i, ctypes.byref(ad)):
				break
			i += 1
			try:
				d = _Desc1()
				_vcall(ad, 10, ctypes.POINTER(_Desc1))(ad, ctypes.byref(d))
				a3 = ctypes.c_void_p()
				if d.flags & 2 or _vcall(ad, 0, ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p))(ad, ctypes.byref(iid_adapter3), ctypes.byref(a3)):
					continue
				self.adapters.append({"name": d.name, "vram": d.vram / MIB, "shared": d.shared / MIB,
					"luid": "0x%08X_0x%08X" % (d.luid.high & 0xFFFFFFFF, d.luid.low),
					"a3": a3, "budget": 0.0, "budget_shared": 0.0})
			finally:
				_vcall(ad, 2)(ad)
		self.refresh()

	def refresh(self):
		for a in self.adapters:
			for seg, key in ((0, "budget"), (1, "budget_shared")):
				m = _MemInfo()
				if _vcall(a["a3"], 14, wt.UINT, wt.UINT, ctypes.POINTER(_MemInfo))(a["a3"], 0, seg, ctypes.byref(m)) == 0:
					a[key] = m.budget / MIB

	def main(self):
		"""The discrete GPU: the adapter with the most dedicated memory."""
		return max(self.adapters, key=lambda a: a["vram"], default=None)


# ---- processes and system memory ---------------------------------------------------------------------------------

class _Pe32(ctypes.Structure):
	_fields_ = [("size", wt.DWORD), ("usage", wt.DWORD), ("pid", wt.DWORD), ("heap", ctypes.c_size_t), ("module", wt.DWORD),
		("threads", wt.DWORD), ("ppid", wt.DWORD), ("pri", wt.LONG), ("flags", wt.DWORD), ("exe", ctypes.c_wchar * 260)]


class _Pmc(ctypes.Structure):
	_fields_ = [("cb", wt.DWORD), ("faults", wt.DWORD), ("peak_ws", ctypes.c_size_t), ("ws", ctypes.c_size_t), ("q1", ctypes.c_size_t),
		("q2", ctypes.c_size_t), ("q3", ctypes.c_size_t), ("q4", ctypes.c_size_t), ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t),
		("private", ctypes.c_size_t), ("private_ws", ctypes.c_size_t), ("shared_commit", ctypes.c_uint64)]


class _MemStatus(ctypes.Structure):
	_fields_ = [("length", wt.DWORD), ("load", wt.DWORD), ("phys", ctypes.c_uint64), ("avail_phys", ctypes.c_uint64), ("commit_limit", ctypes.c_uint64),
		("avail_commit", ctypes.c_uint64), ("virt", ctypes.c_uint64), ("avail_virt", ctypes.c_uint64), ("ext", ctypes.c_uint64)]


class _Perf(ctypes.Structure):
	_fields_ = [("cb", wt.DWORD), ("commit_total", ctypes.c_size_t), ("commit_limit", ctypes.c_size_t), ("commit_peak", ctypes.c_size_t),
		("phys_total", ctypes.c_size_t), ("phys_avail", ctypes.c_size_t), ("sys_cache", ctypes.c_size_t), ("k_total", ctypes.c_size_t),
		("k_paged", ctypes.c_size_t), ("k_nonpaged", ctypes.c_size_t), ("page", ctypes.c_size_t), ("handles", wt.DWORD), ("procs", wt.DWORD), ("threads", wt.DWORD)]


class _SysInfo(ctypes.Structure):
	_fields_ = [("oem_id", wt.DWORD), ("page_size", wt.DWORD), ("min_addr", ctypes.c_void_p), ("max_addr", ctypes.c_void_p),
		("proc_mask", ctypes.c_size_t), ("num_procs", wt.DWORD), ("proc_type", wt.DWORD), ("alloc_gran", wt.DWORD),
		("proc_level", wt.WORD), ("proc_rev", wt.WORD)]


class _Uni(ctypes.Structure):
	_fields_ = [("length", ctypes.c_ushort), ("maxlen", ctypes.c_ushort), ("buffer", ctypes.c_void_p)]


class _Pfi(ctypes.Structure):
	_fields_ = [("next", ctypes.c_ulong), ("size", ctypes.c_ulong), ("used", ctypes.c_ulong), ("peak", ctypes.c_ulong), ("name", _Uni)]


class _Mbi(ctypes.Structure):
	_fields_ = [("base", ctypes.c_void_p), ("alloc_base", ctypes.c_void_p), ("alloc_protect", wt.DWORD), ("pad1", wt.DWORD), ("size", ctypes.c_size_t),
		("state", wt.DWORD), ("protect", wt.DWORD), ("type", wt.DWORD), ("pad2", wt.DWORD)]


_PMC_EX2_SIZE = ctypes.sizeof(_Pmc)
_PMC_EX_SIZE = _Pmc.private_ws.offset
_pmc_cb = _PMC_EX2_SIZE
PAGE_SIZE = 4096
MAX_APP_ADDR = 0x7FFFFFFFFFFF

if WIN:
	_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
	_psapi = ctypes.WinDLL("psapi", use_last_error=True)
	_ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
	_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

	_k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
	_k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
	_k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_Pe32)]
	_k32.Process32FirstW.restype = wt.BOOL
	_k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_Pe32)]
	_k32.Process32NextW.restype = wt.BOOL
	_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
	_k32.OpenProcess.restype = wt.HANDLE
	_k32.CloseHandle.argtypes = [wt.HANDLE]
	_k32.CloseHandle.restype = wt.BOOL
	_k32.GetProcessTimes.argtypes = [wt.HANDLE, ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME)]
	_k32.GetProcessTimes.restype = wt.BOOL
	_k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
	_k32.QueryFullProcessImageNameW.restype = wt.BOOL
	_k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemStatus)]
	_k32.GlobalMemoryStatusEx.restype = wt.BOOL
	_k32.GetSystemInfo.argtypes = [ctypes.POINTER(_SysInfo)]
	_k32.GetSystemInfo.restype = None
	_k32.QueryDosDeviceW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD]
	_k32.QueryDosDeviceW.restype = wt.DWORD
	_k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(_Mbi), ctypes.c_size_t]
	_k32.VirtualQueryEx.restype = ctypes.c_size_t
	_k32.LocalFree.argtypes = [wt.HLOCAL]
	_k32.LocalFree.restype = wt.HLOCAL
	_k32.GetStdHandle.argtypes = [wt.DWORD]
	_k32.GetStdHandle.restype = wt.HANDLE
	_k32.GetConsoleMode.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
	_k32.GetConsoleMode.restype = wt.BOOL
	_k32.SetConsoleMode.argtypes = [wt.HANDLE, wt.DWORD]
	_k32.SetConsoleMode.restype = wt.BOOL

	_psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(_Pmc), wt.DWORD]
	_psapi.GetProcessMemoryInfo.restype = wt.BOOL
	_psapi.GetPerformanceInfo.argtypes = [ctypes.POINTER(_Perf), wt.DWORD]
	_psapi.GetPerformanceInfo.restype = wt.BOOL
	_psapi.GetMappedFileNameW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.LPWSTR, wt.DWORD]
	_psapi.GetMappedFileNameW.restype = wt.DWORD

	_ntdll.NtQuerySystemInformation.argtypes = [wt.ULONG, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG)]
	_ntdll.NtQuerySystemInformation.restype = wt.LONG
	_ntdll.NtQueryInformationProcess.argtypes = [wt.HANDLE, wt.ULONG, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG)]
	_ntdll.NtQueryInformationProcess.restype = wt.LONG
	_ntdll.RtlNtStatusToDosError.argtypes = [wt.LONG]
	_ntdll.RtlNtStatusToDosError.restype = wt.ULONG

	_shell32.CommandLineToArgvW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
	_shell32.CommandLineToArgvW.restype = ctypes.c_void_p

	_si = _SysInfo()
	_k32.GetSystemInfo(ctypes.byref(_si))
	PAGE_SIZE = _si.page_size or 4096
	MAX_APP_ADDR = _si.max_addr or 0x7FFFFFFFFFFF

_DOS = {}


def _rec_err(fn, pid, code=None):
	err = ctypes.get_last_error() if code is None else int(code)
	errors[(fn, pid)] = (err, ctypes.FormatError(err).strip() if err else "unknown error")


def _get_pmc(h, pm):
	global _pmc_cb
	cb = _pmc_cb
	pm.cb = cb
	if _psapi.GetProcessMemoryInfo(h, ctypes.byref(pm), cb):
		if pm.cb < _PMC_EX2_SIZE:
			_pmc_cb = _PMC_EX_SIZE
			return _PMC_EX_SIZE
		return cb
	if cb == _PMC_EX2_SIZE:
		pm.cb = _PMC_EX_SIZE
		if _psapi.GetProcessMemoryInfo(h, ctypes.byref(pm), _PMC_EX_SIZE):
			_pmc_cb = _PMC_EX_SIZE
			return _PMC_EX_SIZE
	return 0


def processes():
	"""{pid: (exe name lowercase, parent pid)}"""
	out = {}
	snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
	if not snap or snap == INVALID_HANDLE_VALUE:
		return out
	pe = _Pe32()
	pe.size = ctypes.sizeof(pe)
	ok = _k32.Process32FirstW(snap, ctypes.byref(pe))
	while ok:
		out[pe.pid] = (pe.exe.lower(), pe.ppid)
		ok = _k32.Process32NextW(snap, ctypes.byref(pe))
	_k32.CloseHandle(snap)
	return out


def proc_mem(pid):
	"""Working set / private working set / commit (MiB), cumulative page faults and CPU seconds; None if the process is gone."""
	h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
	if not h:
		_rec_err("proc_mem", pid)
		return None
	try:
		pm = _Pmc()
		cb = _get_pmc(h, pm)
		if not cb:
			_rec_err("proc_mem", pid)
			return None
		t = [wt.FILETIME() for _ in range(4)]
		if not _k32.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
			_rec_err("proc_mem", pid)
			return None
		cpu = sum(x.dwHighDateTime << 32 | x.dwLowDateTime for x in t[2:]) / 1e7
		pws = pm.private_ws / MIB if cb == _PMC_EX2_SIZE else float("nan")
		errors.pop(("proc_mem", pid), None)
		return {"ws": pm.ws / MIB, "pws": pws, "commit": pm.private / MIB, "faults": pm.faults, "cpu_s": cpu}
	finally:
		_k32.CloseHandle(h)


def proc_info(pid):
	"""{'exe': full image path or None, 'argv': [arguments] or None, 'created': epoch seconds or None}."""
	h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
	if not h:
		_rec_err("proc_info", pid)
		return {"exe": None, "argv": None, "created": None}
	try:
		exe, argv, created, fail_err = None, None, None, None
		size = wt.DWORD(4096)
		buf = ctypes.create_unicode_buffer(size.value)
		if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
			exe = buf.value
		else:
			fail_err = ctypes.get_last_error()
		need = wt.ULONG(0)
		st = _ntdll.NtQueryInformationProcess(h, PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(need))
		if need.value > 0:
			cbuf = ctypes.create_string_buffer(need.value)
			st = _ntdll.NtQueryInformationProcess(h, PROCESS_COMMAND_LINE_INFORMATION, cbuf, need.value, ctypes.byref(need))
			if st == 0:
				u = _Uni.from_buffer(cbuf)
				if u.buffer and u.length > 0:
					cmd = ctypes.wstring_at(u.buffer, u.length // 2)
					argc = ctypes.c_int(0)
					ptr = _shell32.CommandLineToArgvW(cmd, ctypes.byref(argc))
					if ptr:
						try:
							arr = ctypes.cast(ptr, ctypes.POINTER(wt.LPWSTR))
							argv = [arr[i] for i in range(argc.value)]
						finally:
							_k32.LocalFree(ptr)
					elif fail_err is None:
						fail_err = ctypes.get_last_error()
				else:
					argv = []
			elif fail_err is None:
				fail_err = _ntdll.RtlNtStatusToDosError(st)
		elif fail_err is None:
			fail_err = _ntdll.RtlNtStatusToDosError(st)
		t = [wt.FILETIME() for _ in range(4)]
		if _k32.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
			ft = t[0].dwHighDateTime << 32 | t[0].dwLowDateTime
			created = (ft - EPOCH_AS_FILETIME) / 1e7
		elif fail_err is None:
			fail_err = ctypes.get_last_error()
		if fail_err is not None:
			_rec_err("proc_info", pid, fail_err)
		else:
			errors.pop(("proc_info", pid), None)
		return {"exe": exe, "argv": argv, "created": created}
	finally:
		_k32.CloseHandle(h)


def enable_vt():
	"""Enable ANSI virtual terminal processing on stdout; False when stdout is not a console."""
	if not WIN:
		return False
	h = _k32.GetStdHandle(STD_OUTPUT_HANDLE)
	if not h or h == INVALID_HANDLE_VALUE:
		return False
	mode = wt.DWORD(0)
	if not _k32.GetConsoleMode(h, ctypes.byref(mode)):
		return False
	return bool(_k32.SetConsoleMode(h, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))


def pagefile_max():
	"""Configured maximum size of the paging files in MiB, or None when Windows manages them (the limit then grows as needed)."""
	try:
		import winreg

		with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management") as k:
			entries = winreg.QueryValueEx(k, "PagingFiles")[0]
	except OSError:
		return None
	total = 0
	for entry in entries:
		parts = entry.split()
		if len(parts) < 3 or not parts[-1].isdigit() or int(parts[-1]) == 0:
			return None
		total += int(parts[-1])
	return float(total) if entries else None


def sys_mem():
	"""System RAM and commit charge (MiB)."""
	ms, pf = _MemStatus(), _Perf()
	ms.length, pf.cb = ctypes.sizeof(ms), ctypes.sizeof(pf)
	_k32.GlobalMemoryStatusEx(ctypes.byref(ms))
	_psapi.GetPerformanceInfo(ctypes.byref(pf), pf.cb)
	return {"total": ms.phys / MIB, "avail": ms.avail_phys / MIB, "commit": pf.commit_total * pf.page / MIB,
		"commit_limit": pf.commit_limit * pf.page / MIB, "file_cache": pf.sys_cache * pf.page / MIB}


# ---- page files, process address space, per-process RAM -----------------------------------------------------------

def pagefiles():
	"""[{'name', 'size', 'used', 'peak'}] in MiB, from the kernel's page file table."""
	buf = ctypes.create_string_buffer(8192)
	n = wt.ULONG()
	if _ntdll.NtQuerySystemInformation(18, buf, len(buf), ctypes.byref(n)) != 0:
		return []
	out, off = [], 0
	while True:
		e = _Pfi.from_buffer(buf, off)
		name = ctypes.wstring_at(e.name.buffer, e.name.length // 2).replace("\\??\\", "")
		out.append({"name": name, "size": e.size * PAGE_SIZE / MIB, "used": e.used * PAGE_SIZE / MIB, "peak": e.peak * PAGE_SIZE / MIB})
		if not e.next:
			return out
		off += e.next


def pagefile_config():
	"""[(path, initial MiB, maximum MiB)] from the registry; (0, 0) means Windows manages the size."""
	try:
		import winreg

		with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management") as k:
			entries = winreg.QueryValueEx(k, "PagingFiles")[0]
	except OSError:
		return []
	out = []
	for entry in entries:
		parts = entry.split()
		if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
			out.append((" ".join(parts[:-2]), int(parts[-2]), int(parts[-1])))
		elif parts:
			out.append((" ".join(parts), 0, 0))
	return out


def _dos_map():
	if not _DOS:
		buf = ctypes.create_unicode_buffer(520)
		for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
			if _k32.QueryDosDeviceW(letter + ":", buf, 520):
				_DOS[buf.value.lower()] = letter + ":"
	return _DOS


def vm_summary(pid, big_mib=256, name_mib=8):
	"""What a process's address space holds, by walking it (about 150 ms for a large process): MiB committed as private memory, mapped files and
	images, reserved but not committed, the mapped files by name, and the private allocations of at least `big_mib`."""
	h = _k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
	if not h:
		_rec_err("vm_summary", pid)
		return None
	try:
		kinds, files, big, mbi, addr = {"private": 0, "mapped": 0, "image": 0, "reserved": 0}, {}, [], _Mbi(), 0
		buf = ctypes.create_unicode_buffer(520)
		names = _dos_map()
		while addr < MAX_APP_ADDR:
			if not _k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
				break
			size = mbi.size
			if mbi.state == 0x1000:
				kind = {0x40000: "mapped", 0x1000000: "image"}.get(mbi.type, "private")
				kinds[kind] += size
				if kind == "mapped" and size >= name_mib * MIB and _psapi.GetMappedFileNameW(h, ctypes.c_void_p(mbi.base), buf, 520):
					path = buf.value
					for dev, letter in names.items():
						if path.lower().startswith(dev):
							path = letter + path[len(dev):]
							break
					files[path] = files.get(path, 0) + size / MIB
				elif kind == "private" and size >= big_mib * MIB:
					big.append(size / MIB)
			elif mbi.state == 0x2000:
				kinds["reserved"] += size
			addr = (mbi.base or 0) + size
		if addr == 0:
			_rec_err("vm_summary", pid)
			return None
		errors.pop(("vm_summary", pid), None)
		return {**{k: v / MIB for k, v in kinds.items()}, "files": files, "big": sorted(big, reverse=True)}
	finally:
		_k32.CloseHandle(h)


def working_sets(procs):
	"""({exe: (working set MiB, process count)}, total MiB) over every process this user can query."""
	by, total = {}, 0.0
	pmc = _Pmc()
	for pid, (exe, _) in procs.items():
		h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
		if not h:
			continue
		if _get_pmc(h, pmc):
			ws, n = by.get(exe, (0.0, 0))
			by[exe] = (ws + pmc.ws / MIB, n + 1)
			total += pmc.ws / MIB
		_k32.CloseHandle(h)
	return by, total
