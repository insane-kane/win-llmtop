#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for llmtop_lib.state.

	python -I tools/win-llmtop/tests/test_state.py
"""

import contextlib
import http.server
import json
import math
import os
import pathlib
import random
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llmtop_lib import state, win  # noqa: E402


class _StubServer:
	"""Ephemeral HTTP server on 127.0.0.1 for testing Collector."""

	def __init__(self, models_data, slots_data=None, require_key=None, slots_status=200, require_key_on_models=False):
		self.models_data = models_data
		self.slots_data = slots_data if slots_data is not None else [
			{
				"id": 0,
				"n_ctx": 8192,
				"id_task": 7,
				"is_processing": False,
				"n_prompt_tokens": 120,
				"n_prompt_tokens_cache": 80,
				"n_prompt_tokens_processed": 40,
				"next_token": [{"n_decoded": 25, "n_remain": 100}],
				"params": {"temperature": 0.7},
				"speculative": False,
			}
		]
		self.require_key = require_key
		self.require_key_on_models = require_key_on_models
		self.slots_status = slots_status
		self.requests = []
		parent = self

		class Handler(http.server.BaseHTTPRequestHandler):
			def do_GET(self):
				auth = self.headers.get("Authorization")
				parent.requests.append((self.path, auth))
				if self.path == "/models":
					if parent.require_key_on_models and parent.require_key and auth != "Bearer " + parent.require_key:
						self._send_json(401, {"error": {"message": "Invalid API Key", "code": 401}})
						return
					self._send_json(200, {"object": "list", "data": parent.models_data})
					return
				if self.path.startswith("/slots"):
					if parent.require_key and auth != "Bearer " + parent.require_key:
						self._send_json(401, {"error": {"message": "Invalid API Key", "code": 401}})
						return
					if parent.slots_status != 200:
						self._send_json(parent.slots_status, {"error": {"code": parent.slots_status}})
						return
					self._send_json(200, parent.slots_data)
					return
				self._send_json(404, {"error": "not found"})

			def _send_json(self, code, payload):
				raw = json.dumps(payload).encode("utf-8")
				self.send_response(code)
				self.send_header("Content-Type", "application/json")
				self.send_header("Content-Length", str(len(raw)))
				self.end_headers()
				self.wfile.write(raw)

			def log_message(self, fmt, *args):
				pass

		self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
		self.url = "http://127.0.0.1:%d" % self.httpd.server_port
		self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
		self.thread.start()

	def close(self):
		self.httpd.shutdown()
		self.httpd.server_close()


class _StubDxgi:
	def __init__(self, adapters):
		self.adapters = adapters

	def refresh(self):
		pass

	def main(self):
		return max(self.adapters, key=lambda a: a.get("vram", 0.0), default=None)


class _StubCounters:
	def __init__(self, sample_data):
		self.sample_data = sample_data

	def rebuild(self):
		pass

	def sample(self):
		return self.sample_data


def _patch_win(
	procs=None,
	proc_info_map=None,
	proc_mem_map=None,
	adapters=None,
	sample_data=None,
	sys_mem_data=None,
	listener_pid=None,
	guard=True,
):
	"""Patch every Windows call the Collector makes. `listener_pid` is the process that listens on whatever port the watched URL names (None: no one does).

	With `guard`, no program may be started under this patch: `subprocess.run` is replaced, and leaving the block fails the test if it was called.
	"""
	procs = procs if procs is not None else {1000: ("llama-server.exe", 400), 2000: ("llama-server.exe", 1000)}
	proc_info_map = proc_info_map if proc_info_map is not None else {
		1000: {"exe": "llama-server.exe", "argv": ["llama-server.exe", "--port", "8080"], "created": 1700000000.0},
		2000: {"exe": "llama-server.exe", "argv": ["llama-server.exe", "-m", "test.gguf", "-c", "8192"], "created": 1700000010.0},
	}
	proc_mem_map = proc_mem_map if proc_mem_map is not None else {
		1000: {"ws": 50.0, "pws": 40.0, "commit": 60.0, "faults": 10, "cpu_s": 1.0},
		2000: {"ws": 1024.0, "pws": 900.0, "commit": 1100.0, "faults": 100, "cpu_s": 5.0},
	}
	luid = "0x00000000_0x0001C3D4"
	adapters = adapters if adapters is not None else [
		{"name": "Stub Discrete GPU", "vram": 24576.0, "shared": 32768.0, "luid": luid, "budget": 20000.0, "budget_shared": 30000.0}
	]
	sample_data = sample_data if sample_data is not None else {
		"procs": {2000: {"ded": 4096.0, "shr": 128.0, "com": 4224.0, "eng": {"3D": 40.0}}},
		"procs_by_luid": {2000: {luid: {"ded": 4096.0, "shr": 128.0, "com": 4224.0, "eng": {"3D": 40.0}}}},
		"adapters": {luid: {"ded": 4500.0, "shr": 200.0, "com": 4700.0}},
		"sys": {"pages_in": 0.0, "pages_out": 0.0, "standby": 8192.0, "free": 16384.0, "modified": 128.0, "pagefile_pct": 5.0},
	}
	sys_mem_data = sys_mem_data if sys_mem_data is not None else {"total": 65536.0, "avail": 32768.0, "commit": 20000.0, "commit_limit": 81920.0}
	stack = contextlib.ExitStack()
	if guard:
		run_mock = stack.enter_context(mock.patch("subprocess.run", side_effect=AssertionError("a program was started")))
		stack.callback(lambda: run_mock.assert_not_called())
	stack.enter_context(mock.patch.object(win, "WIN", True))
	stack.enter_context(mock.patch.object(win, "tcp_listener_pid", create=True, side_effect=lambda port, host=None: listener_pid))  # create: the suite also runs against code that predates the function
	stack.enter_context(mock.patch.object(win, "Dxgi", side_effect=lambda: _StubDxgi(adapters)))
	stack.enter_context(mock.patch.object(win, "Counters", side_effect=lambda: _StubCounters(sample_data)))
	stack.enter_context(mock.patch.object(win, "processes", side_effect=lambda: dict(procs)))
	stack.enter_context(mock.patch.object(win, "proc_info", side_effect=lambda pid: proc_info_map.get(pid, {"exe": None, "argv": None, "created": None})))
	stack.enter_context(mock.patch.object(win, "proc_mem", side_effect=lambda pid: proc_mem_map.get(pid)))
	stack.enter_context(mock.patch.object(win, "sys_mem", side_effect=lambda: dict(sys_mem_data)))
	stack.enter_context(mock.patch.object(win, "pagefile_max", return_value=16384.0))
	stack.enter_context(mock.patch.object(win, "pagefile_config", return_value=[]))
	stack.enter_context(mock.patch.object(win, "pagefiles", return_value=[]))
	stack.enter_context(mock.patch.object(win, "working_sets", return_value=({}, 0.0)))
	stack.enter_context(mock.patch.object(win, "vm_summary", return_value={"private": 1100.0, "mapped": 100.0, "image": 20.0, "reserved": 50.0, "files": {}, "big": []}))
	return stack


class TestState(unittest.TestCase):
	def test_router_mode_preset_loaded_and_flags_parsed(self):
		models_data = [
			{
				"id": "org/repo:q4_k_m",
				"object": "model",
				"status": {
					"value": "loaded",
					"args": ["llama-server.exe", "--model", "model.gguf", "-c", "16384", "-ctk", "q8_0", "-ctv", "q4_0"],
				},
				"meta": {"n_params": 7000000000},
			}
		]
		srv = _StubServer(models_data)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=tmp, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				snap = c.tick(1700000100.0)
				self.assertEqual(snap["mode"], "router")
				self.assertTrue(snap["router_up"])
				self.assertIn("org/repo:q4_k_m", snap["presets"])
				p = snap["presets"]["org/repo:q4_k_m"]
				self.assertEqual(p["status"], "loaded")
				self.assertIsNotNone(p["slot"])
				self.assertEqual(p["slot"]["n_ctx"], 8192)
				flags, exe = c.flags["org/repo:q4_k_m"]
				self.assertIsNone(exe)  # args[0] is not a trusted program: no process listens on the stub's port as llama-server
				self.assertTrue(any("no helper program is run" in n for n in snap["notices"]), snap["notices"])
				self.assertEqual(flags.get("ctx-size"), "16384")
				self.assertEqual(flags.get("cache-type-k"), "q8_0")
				self.assertEqual(flags.get("cache-type-v"), "q4_0")
				self.assertEqual(os.path.basename(c.preset_log_path("org/repo:q4_k_m")), "org_repo_q4_k_m.log")
				self.assertTrue(any(path.startswith("/slots?model=") for path, _ in srv.requests))
		finally:
			srv.close()

	def test_single_model_mode_process_counted(self):
		models_data = [
			{
				"id": "local-single.gguf",
				"object": "model",
				"owned_by": "llamacpp",
				"meta": {"n_params": 8000000000},
			}
		]
		srv = _StubServer(models_data)
		luid = "0x00000000_0x0001C3D4"
		procs = {3456: ("llama-server.exe", 500)}
		proc_info_map = {
			3456: {
				"exe": "C:\\bin\\llama-server.exe",
				"argv": ["C:\\bin\\llama-server.exe", "-m", "local-single.gguf", "-c", "32768", "--flash-attn", "on"],
				"created": 1700000050.0,
			}
		}
		proc_mem_map = {3456: {"ws": 2048.0, "pws": 1900.0, "commit": 2200.0, "faults": 50, "cpu_s": 12.0}}
		sample_data = {
			"procs": {3456: {"ded": 6144.0, "shr": 64.0, "com": 6208.0, "eng": {"3D": 25.0}}},
			"procs_by_luid": {3456: {luid: {"ded": 6144.0, "shr": 64.0, "com": 6208.0, "eng": {"3D": 25.0}}}},
			"adapters": {luid: {"ded": 6400.0, "shr": 100.0, "com": 6500.0}},
			"sys": {"pages_in": 0.0, "pages_out": 0.0, "standby": 8192.0, "free": 16384.0, "modified": 128.0, "pagefile_pct": 5.0},
		}
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win(procs=procs, proc_info_map=proc_info_map, proc_mem_map=proc_mem_map, sample_data=sample_data, listener_pid=3456):
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				snap = c.tick(1700000100.0)
				self.assertEqual(snap["mode"], "single")
				self.assertIn("single-model mode", snap["notices"])
				self.assertTrue(snap["llama"]["present"])
				self.assertEqual(snap["llama"]["pids"], [3456])
				self.assertEqual(snap["llama"]["ded"], 6144.0)
				self.assertEqual(snap["llama"]["ws"], 2048.0)
				self.assertEqual(snap["presets"]["local-single.gguf"]["status"], "loaded")
				flags, exe = c.flags["local-single.gguf"]
				self.assertEqual(exe, "C:\\bin\\llama-server.exe")  # the trusted program: the one listening on the watched port
				self.assertFalse(any("no helper program is run" in n for n in snap["notices"]), snap["notices"])
				self.assertEqual(flags.get("ctx-size"), "32768")
				self.assertEqual(flags.get("flash-attn"), "on")
				self.assertIn(("/slots", None), srv.requests)
		finally:
			srv.close()

	def test_router_flagless_loaded_model_no_crash_and_notice(self):
		# a router whose /models lists a loaded model with no status.args and no matching process
		models_data = [
			{
				"id": "flagless-preset",
				"object": "model",
				"status": {"value": "loaded"},
			}
		]
		srv = _StubServer(models_data)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win(procs={}):
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				snap = c.tick(1700000100.0)
				self.assertEqual(snap["mode"], "router")
				self.assertEqual(snap["presets"]["flagless-preset"]["status"], "loaded")
				self.assertNotIn("flagless-preset", c.flags)  # no status.args, no process
				self.assertFalse(snap["llama"]["present"])
				self.assertTrue(any("no process named llama-server* found" in n for n in snap["notices"]))
				# the crash path: pick_model calls ensure_map for the loaded model
				c.ensure_map("flagless-preset")  # must not raise
				entry = c.mem["flagless-preset"]
				self.assertEqual(entry["state"], "error")
				self.assertIsNone(entry["key"])
				self.assertEqual(entry["error"], state.Collector.NO_FLAGS_ERROR)
				c.ensure_map("flagless-preset")  # a second call must not raise or rewrite
				self.assertEqual(c.mem["flagless-preset"]["state"], "error")
		finally:
			srv.close()

	def test_single_model_process_not_found_notice_and_no_crash(self):
		models_data = [
			{
				"id": "local-single.gguf",
				"object": "model",
				"owned_by": "llamacpp",
				"meta": {"n_params": 8000000000},
			}
		]
		srv = _StubServer(models_data)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win(procs={}):
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				snap = c.tick(1700000100.0)
				self.assertEqual(snap["mode"], "single")
				self.assertIn("single-model mode", snap["notices"])
				self.assertEqual(snap["presets"]["local-single.gguf"]["status"], "loaded")
				self.assertNotIn("local-single.gguf", c.flags)  # no process to find flags from
				self.assertFalse(snap["llama"]["present"])
				self.assertTrue(any("no process named llama-server* found" in n for n in snap["notices"]))
				c.ensure_map("local-single.gguf")  # must not raise
				self.assertEqual(c.mem["local-single.gguf"]["state"], "error")
				self.assertEqual(c.mem["local-single.gguf"]["error"], state.Collector.NO_FLAGS_ERROR)
		finally:
			srv.close()

	def test_slots_401_and_api_key_header(self):
		models_data = [
			{
				"id": "preset-a",
				"object": "model",
				"status": {"value": "loaded", "args": ["llama-server.exe", "-m", "a.gguf"]},
			}
		]
		srv = _StubServer(models_data, require_key="test-token-123")
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, api_key=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				snap1 = c.tick(1700000100.0)
				p1 = snap1["presets"]["preset-a"]
				self.assertEqual(p1["slot_error"], "HTTP 401")
				self.assertIsNone(p1["slot"])
				self.assertIn("slots:preset-a", c.alerts.active)
				alert = c.alerts.active["slots:preset-a"]
				self.assertIn("HTTP 401", alert["msg"])  # the headline names the error
				self.assertIn("--api-key", alert["detail"])  # the fix moved to the detail
				self.assertIn("LLAMA_API_KEY", alert["detail"])

				c.cfg["api_key"] = "test-token-123"
				snap2 = c.tick(1700000100.5)
				p2 = snap2["presets"]["preset-a"]
				self.assertIsNone(p2["slot_error"])
				self.assertIsNotNone(p2["slot"])
				self.assertEqual(p2["slot"]["n_ctx"], 8192)
				self.assertTrue(any(auth == "Bearer test-token-123" for path, auth in srv.requests if path.startswith("/slots")))
				self.assertNotIn("slots:preset-a", c.alerts.active)
		finally:
			srv.close()

	def test_uma_zero_vram_adapter(self):
		models_data = [{"id": "uma-model", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "u.gguf"]}}]
		srv = _StubServer(models_data)
		luid_uma = "0x00000000_0x0000A1B2"
		adapters = [{"name": "Integrated UMA Graphics", "vram": 0.0, "shared": 16384.0, "luid": luid_uma, "budget": 0.0, "budget_shared": 14000.0}]
		sample_data = {
			"procs": {2000: {"ded": 0.0, "shr": 3072.0, "com": 3072.0, "eng": {"3D": 50.0}}},
			"procs_by_luid": {2000: {luid_uma: {"ded": 0.0, "shr": 3072.0, "com": 3072.0, "eng": {"3D": 50.0}}}},
			"adapters": {luid_uma: {"ded": 0.0, "shr": 3500.0, "com": 3500.0}},
			"sys": {"pages_in": 0.0, "pages_out": 0.0, "standby": 4096.0, "free": 8192.0, "modified": 64.0, "pagefile_pct": 0.0},
		}
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win(adapters=adapters, sample_data=sample_data):
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				snap = c.tick(1700000100.0)
				self.assertIsNotNone(snap["gpu"])
				self.assertTrue(snap["gpu"]["uma"])
				self.assertEqual(snap["gpu"]["vram"], 0.0)
				self.assertEqual(snap["gpu"]["budget"], 14000.0)
				self.assertNotIn("full", c.alerts.active)
		finally:
			srv.close()

	def test_high_page_in_rate_raises_no_alert(self):
		models_data = []
		srv = _StubServer(models_data)
		luid = "0x00000000_0x0001C3D4"
		sample_data = {
			"procs": {},
			"procs_by_luid": {},
			"adapters": {luid: {"ded": 0.0, "shr": 0.0, "com": 0.0}},
			"sys": {"pages_in": 10000.0, "pages_out": 500.0, "standby": 8192.0, "free": 16384.0, "modified": 128.0, "pagefile_pct": 5.0},
		}
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win(procs={}, proc_info_map={}, proc_mem_map={}, sample_data=sample_data):
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				snap = c.tick(1700000100.0)
				self.assertEqual(snap["ram"]["pages_in"], 10000.0)
				self.assertNotIn("faults", c.alerts.active)
				self.assertFalse(any("page faults" in a["msg"] for a in c.alerts.active.values()))
		finally:
			srv.close()

	def test_invalid_state_dir_disables_persistence_with_notice(self):
		models_data = []
		srv = _StubServer(models_data)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				blocker = os.path.join(tmp, "not_a_dir.txt")
				with open(blocker, "w", encoding="utf-8") as f:
					f.write("file blocking directory creation")
				bad_state_dir = os.path.join(blocker, "sub")
				cfg = dict(state.CFG, router=srv.url, state_dir=bad_state_dir, log_dir=None, persist=True, calibrate=True)
				c = state.Collector(cfg)
				c.load_history()
				snap = c.tick(1700000100.0)
				self.assertFalse(c.cfg["persist"])
				self.assertFalse(c.cfg["calibrate"])
				self.assertTrue(any("state folder" in n and "unavailable" in n for n in snap["notices"]), snap["notices"])
		finally:
			srv.close()

	def test_log_dir_none_and_no_router_log_touches_no_folder(self):
		models_data = [{"id": "m1", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "m1.gguf"]}}]
		srv = _StubServer(models_data)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				uncreated_state = os.path.join(tmp, "uncreated_state")
				cfg = dict(state.CFG, router=srv.url, state_dir=uncreated_state, log_dir=None, router_log=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				c.load_history()
				snap = c.tick(1700000100.0)
				self.assertEqual(os.listdir(tmp), [])
				self.assertFalse(os.path.exists(uncreated_state))
				self.assertTrue(any("no log file for preset m1" in n for n in snap["notices"]), snap["notices"])
		finally:
			srv.close()

	def test_threshold_arithmetic_8_16_64_gib(self):
		for ram_gib, exp_avail_warn, exp_avail_crit in (
			(8, 573, 287),
			(16, 1147, 573),
			(64, 4096, 2048),
		):
			ram_mib = ram_gib * 1024
			commit_max = ram_mib + 16384
			budget = 19456
			cfg = dict(state.CFG)
			state.scale_thresholds(cfg, ram_mib=ram_mib, commit_max_mib=commit_max, budget_mib=budget)
			self.assertEqual(cfg["avail_warn"], exp_avail_warn)
			self.assertEqual(cfg["avail_crit"], exp_avail_crit)
			self.assertEqual(cfg["commit_warn"], min(8192, round(0.10 * commit_max)))
			self.assertEqual(cfg["commit_crit"], min(3072, round(0.04 * commit_max)))
			self.assertEqual(cfg["near_budget_mib"], 512)
			self.assertEqual(cfg["idle_headroom_mib"], 1024)

		cfg_override = dict(state.CFG, avail_warn=999)
		state.scale_thresholds(cfg_override, ram_mib=8192, commit_max_mib=16384, budget_mib=8192, user_set={"avail_warn"})
		self.assertEqual(cfg_override["avail_warn"], 999)
		self.assertEqual(cfg_override["avail_crit"], 287)
		self.assertEqual(cfg_override["near_budget_mib"], round(0.03 * 8192))
		self.assertEqual(cfg_override["idle_headroom_mib"], round(0.06 * 8192))

	def test_discovery_gpu_selection_errors_and_multi_resident(self):
		models_data = [
			{"id": "p1", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "p1.gguf"]}},
			{"id": "p2", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "p2.gguf"]}},
		]
		srv = _StubServer(models_data, slots_status=501)
		port = str(srv.httpd.server_port)
		luid0 = "0x00000000_0x00001111"
		luid1 = "0x00000000_0x00002222"
		adapters = [
			{"name": "Adapter Zero", "vram": 16384.0, "shared": 32768.0, "luid": luid0, "budget": 15000.0, "budget_shared": 30000.0},
			{"name": "Adapter One", "vram": 12288.0, "shared": 32768.0, "luid": luid1, "budget": 11000.0, "budget_shared": 30000.0},
		]
		procs = {1000: ("llama-server.exe", 400), 2001: ("llama-server.exe", 1000), 2002: ("llama-server.exe", 1000), 9999: ("game.exe", 400)}
		proc_info_map = {
			1000: {"exe": "llama-server.exe", "argv": ["llama-server.exe", "--host", "0.0.0.0", "--port", port], "created": 1700000000.0},
			2001: {"exe": "llama-server.exe", "argv": ["llama-server.exe", "-m", "p1.gguf"], "created": 1700000010.0},
			2002: {"exe": "llama-server.exe", "argv": ["llama-server.exe", "-m", "p2.gguf"], "created": 1700000020.0},
		}
		sample_data = {
			"procs": {
				2001: {"ded": 3000.0, "shr": 100.0, "com": 3100.0, "eng": {"3D": 10.0}},
				2002: {"ded": 2000.0, "shr": 50.0, "com": 2050.0, "eng": {"3D": 20.0}},
				9999: {"ded": 500.0, "shr": 0.0, "com": 500.0, "eng": {"3D": 80.0}},
			},
			"procs_by_luid": {
				2001: {luid1: {"ded": 3000.0, "shr": 100.0, "com": 3100.0, "eng": {"3D": 10.0}}},
				2002: {luid1: {"ded": 2000.0, "shr": 50.0, "com": 2050.0, "eng": {"3D": 20.0}}},
				9999: {
					luid0: {"ded": 400.0, "shr": 0.0, "com": 400.0, "eng": {"3D": 80.0}},
					luid1: {"ded": 100.0, "shr": 0.0, "com": 100.0, "eng": {"3D": 5.0}},
				},
			},
			"adapters": {
				luid0: {"ded": 400.0, "shr": 0.0, "com": 400.0},
				luid1: {"ded": 5100.0, "shr": 150.0, "com": 5250.0},
			},
			"sys": {"pages_in": 0.0, "pages_out": 0.0, "standby": 4096.0, "free": 8192.0, "modified": 64.0, "pagefile_pct": 0.0},
		}
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win(procs=procs, proc_info_map=proc_info_map, proc_mem_map={}, adapters=adapters, sample_data=sample_data):
				win.errors[("proc_mem", 2002)] = (5, "Access is denied.")
				win.errors[("vm_summary", 2001)] = (5, "Access is denied.")
				cfg = dict(state.CFG, router=None, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=True)
				c = state.Collector(cfg)
				c.cfg["router"] = "http://127.0.0.1:1"
				c.ensure_map = lambda preset: None
				c.vm = {"private": 500.0, "pid": 2001, "at": 1700000000.0}
				c.due["vm"] = 1700000200.0
				with mock.patch.object(win, "vm_summary", return_value=None):
					c.vm_job(2001)
				self.assertEqual(c.vm["private"], 500.0)
				self.assertIn("Access is denied.", c.vm_error)

				snap = c.tick(1700000100.0)
				self.assertEqual(c.cfg["router"], srv.url)
				self.assertEqual(snap["gpu"]["name"], "Adapter One")
				self.assertEqual(snap["llama"]["ded"], 5000.0)
				self.assertEqual(snap["others"][0]["ded"], 100.0)
				self.assertEqual(snap["others"][0]["eng"], 5.0)
				self.assertIn("Access is denied.", snap["llama"]["pm_error"])
				self.assertIn("Access is denied.", snap["vm_error"])
				self.assertEqual(snap["presets"]["p1"]["slot_error"], "HTTP 501")
				self.assertIn("HTTP 501", c.alerts.active["slots:p1"]["msg"])
				self.assertIn("--slots", c.alerts.active["slots:p1"]["detail"])
				self.assertTrue(any("calibration is paused" in n for n in snap["notices"]), snap["notices"])
		finally:
			srv.close()


	def test_models_401_discovery_and_auth_alert(self):
		models_data = [
			{
				"id": "auth-model.gguf",
				"object": "model",
				"owned_by": "llamacpp",
			}
		]
		srv = _StubServer(models_data, require_key="real-key-456", require_key_on_models=True)
		port = str(srv.httpd.server_port)
		procs = {4321: ("llama-server.exe", 500)}
		proc_info_map = {
			4321: {
				"exe": "C:\\bin\\llama-server.exe",
				"argv": ["C:\\bin\\llama-server.exe", "-m", "auth-model.gguf", "--port", port],
				"created": 1700000050.0,
			}
		}
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win(procs=procs, proc_info_map=proc_info_map, proc_mem_map={}):
				cfg = dict(state.CFG, router=None, state_dir=os.path.join(tmp, "state"), log_dir=None, api_key="wrong", persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.cfg["router"] = "http://127.0.0.1:1"
				c.ensure_map = lambda preset: None
				snap1 = c.tick(1700000100.0)
				self.assertEqual(c.cfg["router"], srv.url)
				self.assertFalse(snap1["router_up"])
				self.assertEqual(snap1["router_auth_error"], "HTTP 401")
				self.assertIsNotNone(c.router_discovery)
				self.assertIn(c.router_discovery, snap1["notices"])
				self.assertFalse(any("server unreachable" in n for n in snap1["notices"]), snap1["notices"])
				self.assertIn("router", c.alerts.active)
				alert_msg = c.alerts.active["router"]["msg"]
				self.assertIn("HTTP 401", alert_msg)
				self.assertIn("--api-key", alert_msg)
				self.assertIn("LLMTOP_API_KEY", alert_msg)
				self.assertIn("LLAMA_API_KEY", alert_msg)
				self.assertNotIn("unreachable", alert_msg)

				c.cfg["api_key"] = "real-key-456"
				snap2 = c.tick(1700000100.5)
				self.assertTrue(snap2["router_up"])
				self.assertIsNone(snap2["router_auth_error"])
				c.alerts.sweep(1700000100.0 + state.CFG["clear_s"] + 1)  # the alert clears after clear_s of quiet, like every other
				self.assertNotIn("router", c.alerts.active)
				self.assertEqual(snap2["mode"], "single")
				self.assertEqual(snap2["presets"]["auth-model.gguf"]["slot"]["n_ctx"], 8192)
		finally:
			srv.close()

	def test_multiple_independent_servers_and_single_model_pid_matching(self):
		models_data = [
			{
				"id": "target.gguf",
				"object": "model",
				"owned_by": "llamacpp",
			}
		]
		srv = _StubServer(models_data)
		port = str(srv.httpd.server_port)
		procs = {
			1000: ("llama-server.exe", 400),
			1001: ("llama-server.exe", 1000),
			3000: ("llama-server.exe", 400),
		}
		proc_info_map = {
			1000: {
				"exe": "C:\\bin\\llama-server.exe",
				"argv": ["C:\\bin\\llama-server.exe", "--models-preset", "presets.ini", "--log-file", "C:\\logs\\router.log"],
				"created": 1700000000.0,
			},
			1001: {
				"exe": "C:\\bin\\llama-server.exe",
				"argv": ["C:\\bin\\llama-server.exe", "-m", "child.gguf", "--port", "59001", "--log-file", "C:\\logs\\child.log"],
				"created": 1700000010.0,
			},
			3000: {
				"exe": "C:\\bin\\llama-server.exe",
				"argv": ["C:\\bin\\llama-server.exe", "-m", "target.gguf", "--port", port, "-c", "16384", "--log-file", "C:\\logs\\target.log"],
				"created": 1700000020.0,
			},
		}
		proc_mem_map = {
			1000: {"ws": 50.0, "pws": 40.0, "commit": 60.0, "faults": 1, "cpu_s": 1.0},
			1001: {"ws": 8000.0, "pws": 7900.0, "commit": 8100.0, "faults": 5, "cpu_s": 10.0},
			3000: {"ws": 512.0, "pws": 500.0, "commit": 600.0, "faults": 2, "cpu_s": 2.0},
		}
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win(procs=procs, proc_info_map=proc_info_map, proc_mem_map=proc_mem_map, listener_pid=3000):
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				c.load_history()
				snap = c.tick(1700000100.0)
				self.assertEqual(snap["mode"], "single")
				self.assertEqual(snap["llama"]["pids"], [3000])
				self.assertEqual(snap["llama"]["ws"], 512.0)
				self.assertEqual(c.preset_log_path("target.gguf"), "C:\\logs\\target.log")
				self.assertIsNone(c.router_tail)
				flags, _ = c.flags["target.gguf"]
				self.assertEqual(flags.get("ctx-size"), "16384")
				expected_notice = "2 llama-server instances found; watching %s (pass --router to choose)" % srv.url
				self.assertIn(expected_notice, snap["notices"])

			# When multiple independent servers exist and none matches the URL's port, never take another server's flags or log.
			unmatched_info = dict(proc_info_map)
			unmatched_info[3000] = {
				"exe": "C:\\bin\\llama-server.exe",
				"argv": ["C:\\bin\\llama-server.exe", "-m", "other.gguf", "--port", "8081", "--log-file", "C:\\logs\\other.log"],
				"created": 1700000020.0,
			}
			with tempfile.TemporaryDirectory() as tmp, _patch_win(procs=procs, proc_info_map=unmatched_info, proc_mem_map=proc_mem_map):
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c2 = state.Collector(cfg)
				c2.ensure_map = lambda preset: None
				snap2 = c2.tick(1700000100.0)
				self.assertEqual(snap2["llama"]["pids"], [])
				self.assertNotIn("target.gguf", c2.flags)
				self.assertIsNone(c2.preset_log_path("target.gguf"))
		finally:
			srv.close()

	def test_three_slots_only_slot_2_processing(self):
		slots_data = [
			{"id": 0, "n_ctx": 8192, "id_task": 1, "is_processing": False, "n_prompt_tokens": 50, "next_token": [{"n_decoded": 0}]},
			{"id": 1, "n_ctx": 8192, "id_task": 2, "is_processing": False, "n_prompt_tokens": 60, "next_token": [{"n_decoded": 0}]},
			{"id": 2, "n_ctx": 8192, "id_task": 3, "is_processing": True, "n_prompt_tokens": 100, "n_prompt_tokens_cache": 0, "n_prompt_tokens_processed": 100, "next_token": [{"n_decoded": 25, "n_remain": 75}]},
		]
		models_data = [{"id": "m1", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "m1.gguf"]}}]
		srv = _StubServer(models_data, slots_data=slots_data)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				snap = c.tick(1700000100.0)
				self.assertTrue(snap["busy"])
				p = snap["presets"]["m1"]
				self.assertEqual(p["phase"], "decode")
				self.assertEqual(p["slot"]["id"], 2)
				self.assertEqual(p["n_slots"], 3)
				self.assertEqual(p["n_busy"], 1)
		finally:
			srv.close()

	def test_two_slots_processing_primary_is_newer(self):
		slots_data = [
			{"id": 0, "n_ctx": 8192, "id_task": 10, "is_processing": True, "n_prompt_tokens": 50, "n_prompt_tokens_cache": 0, "n_prompt_tokens_processed": 50, "next_token": [{"n_decoded": 10, "n_remain": 40}]},
			{"id": 1, "n_ctx": 8192, "id_task": 20, "is_processing": True, "n_prompt_tokens": 80, "n_prompt_tokens_cache": 0, "n_prompt_tokens_processed": 80, "next_token": [{"n_decoded": 5, "n_remain": 75}]},
			{"id": 2, "n_ctx": 8192, "id_task": 5, "is_processing": False, "n_prompt_tokens": 30, "next_token": [{"n_decoded": 0}]},
		]
		models_data = [{"id": "m1", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "m1.gguf"]}}]
		srv = _StubServer(models_data, slots_data=slots_data)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				snap = c.tick(1700000100.0)
				self.assertTrue(snap["busy"])
				p = snap["presets"]["m1"]
				self.assertEqual(p["n_slots"], 3)
				self.assertEqual(p["n_busy"], 2)
				self.assertEqual(p["slot"]["id"], 1)  # newer task id 20 vs 10
		finally:
			srv.close()

	def test_per_slot_rates_separate(self):
		slots_state = [
			{"id": 0, "n_ctx": 8192, "id_task": 10, "is_processing": True, "n_prompt_tokens": 100, "n_prompt_tokens_cache": 0, "n_prompt_tokens_processed": 100, "next_token": [{"n_decoded": 0, "n_remain": 100}]},
			{"id": 1, "n_ctx": 8192, "id_task": 11, "is_processing": True, "n_prompt_tokens": 100, "n_prompt_tokens_cache": 0, "n_prompt_tokens_processed": 100, "next_token": [{"n_decoded": 0, "n_remain": 100}]},
		]
		models_data = [{"id": "m1", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "m1.gguf"]}}]
		srv = _StubServer(models_data, slots_data=slots_state)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False, hz=4.0)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				# Tick over 1 second, advancing slot 0 by 10 t/s and slot 1 by 50 t/s
				for step in range(5):
					t = 1700000100.0 + step * 0.25
					c.due.clear()
					slots_state[0]["next_token"] = [{"n_decoded": int(10 * step * 0.25), "n_remain": 100}]
					slots_state[1]["next_token"] = [{"n_decoded": int(50 * step * 0.25), "n_remain": 100}]
					c.tick(t)
				r0 = c.rate.get(("m1", 0))
				r1 = c.rate.get(("m1", 1))
				self.assertIsNotNone(r0)
				self.assertIsNotNone(r1)
				self.assertAlmostEqual(r0["dec"], 10.0, delta=2.0)
				self.assertAlmostEqual(r1["dec"], 50.0, delta=2.0)
		finally:
			srv.close()

	def test_slots_reply_bad_types_and_empty(self):
		models_data = [{"id": "m1", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "m1.gguf"]}}]
		srv = _StubServer(models_data)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None

				# 1. Dict reply
				srv.slots_data = {"error": "bad request"}
				c.due.clear()
				snap = c.tick(1700000100.0)
				self.assertEqual(snap["presets"]["m1"]["slot_error"], "unexpected /slots reply")

				# 2. List of strings
				srv.slots_data = ["slot0", "slot1"]
				c.due.clear()
				snap = c.tick(1700000101.0)
				self.assertEqual(snap["presets"]["m1"]["slot_error"], "unexpected /slots reply")

				# 3. Empty list
				srv.slots_data = []
				c.due.clear()
				snap = c.tick(1700000102.0)
				self.assertIsNone(snap["presets"]["m1"]["slot_error"])
				self.assertEqual(snap["presets"]["m1"]["phase"], "idle")
				self.assertEqual(snap["presets"]["m1"]["n_slots"], 0)
				self.assertEqual(snap["presets"]["m1"]["n_busy"], 0)
		finally:
			srv.close()


class _Emit:
	"""A stand-in for Collector.emit: records each event it is called with."""

	def __init__(self):
		self.events = []

	def __call__(self, level, kind, msg, wall=None, preset=None, detail=None):
		self.events.append({"level": level, "kind": kind, "msg": msg, "detail": detail})


def _snap(**over):
	"""A snapshot that triggers no alert; `over` replaces whole sections."""
	s = {
		"t": 1700000100.0,
		"router_up": True,
		"router_error": "",
		"router_auth_error": None,
		"presets": {},
		"gpu": {"name": "Stub GPU", "vram": 24576.0, "shared": 32768.0, "budget": 20000.0, "ded": 4500.0, "shr": 200.0, "uma": False},
		"llama": {
			"present": True, "pids": [2000], "ded": 4096.0, "shr": 128.0, "com": 4224.0, "ws": 1024.0, "commit": 1100.0,
			"age": 1000.0, "floor": 0.0, "floor_known": False, "allow": 0.0, "allow_known": False, "allow_src": "",
			"over": 0.0, "ws_drop": 0.0, "commit_drop": 0.0, "vm": None, "vm_error": None,
		},
		"ram": {"total": 65536.0, "avail": 32768.0, "commit": 20000.0, "commit_limit": 81920.0, "commit_max": 98304.0, "pagefiles": []},
		"others": [],
		"busy": False,
		"new_events": [],
		"notices": [],
	}
	for k, v in over.items():
		s[k] = v
	return s


class TestAlertHeadlines(unittest.TestCase):
	def _alerts(self, snaps):
		"""Run Alerts.evaluate over `snaps` in order; return (alerts, emit)."""
		emit = _Emit()
		a = state.Alerts(dict(state.CFG, router="http://127.0.0.1:8080"), emit)
		for s in snaps:
			a.evaluate(s, None)
		return a, emit

	def _one(self, key, snaps):
		a, emit = self._alerts(snaps)
		self.assertIn(key, a.active, "alert %r did not fire" % key)
		return a.active[key], emit

	def test_every_alert_headline_fits_in_72_chars(self):
		# (name, key, snapshots) for every alert rule in Alerts.evaluate
		ll = lambda **kw: dict(_snap()["llama"], **kw)
		cases = [
			("router auth", "router", [_snap(router_up=False, router_auth_error="HTTP 401")]),
			("router unreachable", "router", [_snap(router_up=False, router_error="connection refused")]),
			("slots", "slots:preset-a", [_snap(presets={"preset-a": {"slot_error": "HTTP 401"}})]),
			("spill excess", "spill", [_snap(llama=ll(shr=428.0, allow=128.0, over=300.0, allow_known=True))]),
			("spill grew", "spill", [_snap(llama=ll(shr=512.0, floor=128.0, floor_known=True))]),
			("burst crit", "burst", [_snap(t=1700000000.0, llama=ll(shr=128.0, ded=4096.0)), _snap(t=1700000002.0, llama=ll(shr=328.0, ded=3946.0))]),
			("burst warn", "burst", [_snap(t=1700000000.0, llama=ll(shr=128.0, ded=4096.0)), _snap(t=1700000002.0, llama=ll(shr=328.0, ded=4096.0))]),
			("return", "return", [_snap(t=1700000000.0, llama=ll(shr=528.0, ded=4096.0)), _snap(t=1700000002.0, llama=ll(shr=328.0, ded=4096.0))]),
			("paged", "paged", [_snap(llama=ll(com=4600.0))]),
			("budget", "budget", [_snap(llama=ll(ded=19980.0))]),
			("rival", "rival", [_snap(others=[{"exe": "chrome.exe", "eng": 45.0, "ded": 100.0}], busy=True)]),
			("full", "full", [_snap(gpu=dict(_snap()["gpu"], ded=24000.0))]),
			("ram crit", "ram", [_snap(ram=dict(_snap()["ram"], avail=1000.0))]),
			("ram warn", "ram", [_snap(ram=dict(_snap()["ram"], avail=3000.0))]),
			("commit crit", "commit", [_snap(ram=dict(_snap()["ram"], commit=96000.0))]),
			("commit warn", "commit", [_snap(ram=dict(_snap()["ram"], commit=92000.0))]),
			("commit info", "commit", [_snap(ram=dict(_snap()["ram"], commit=75000.0))]),
			("pagefile", "pagefile", [_snap(ram=dict(_snap()["ram"], pagefiles=[{"name": "C:\\pagefile.sys", "size": 16384.0, "used": 9000.0}]))]),
			("trim info", "trim", [_snap(llama=ll(ws_drop=2048.0, commit_drop=1024.0))]),
			("trim warn", "trim", [_snap(llama=ll(ws_drop=2048.0, commit_drop=100.0))]),
			("exit", "exit", [_snap(new_events=[{"kind": "exit", "level": "crit", "msg": "model-a exited with status 1"}])]),
		]
		for name, key, snaps in cases:
			with self.subTest(name=name):
				alert, _ = self._one(key, snaps)
				self.assertLessEqual(len(alert["msg"]), 72, alert["msg"])

	def test_detail_carried_through_flag_and_emit(self):
		emit = _Emit()
		a = state.Alerts(dict(state.CFG, router="http://127.0.0.1:8080"), emit)
		a.flag("test", "warn", "a headline", 1000.0, detail="the detail")
		self.assertEqual(a.active["test"]["detail"], "the detail")  # kept on the active alert
		self.assertEqual(emit.events[0]["detail"], "the detail")  # carried into the emitted event
		a.flag("test", "warn", "a headline", 1010.0, detail="a new detail")  # a refresh updates it
		self.assertEqual(a.active["test"]["detail"], "a new detail")
		self.assertEqual(len(emit.events), 1)  # ... without re-emitting
		a.flag("plain", "info", "no detail here", 1020.0)
		self.assertIsNone(emit.events[1]["detail"])

	def test_detail_persisted_and_loaded(self):
		with tempfile.TemporaryDirectory() as tmp:
			h = state.History(tmp)
			ev = {"wall": 1700000100.0, "t": 0, "level": "warn", "kind": "alert", "msg": "a headline", "detail": "the detail", "preset": None, "session": None}
			h.write_event(ev)
			h.close()
			samples, events = state.History(tmp).load(0)
			self.assertEqual(len(events), 1)
			self.assertEqual(events[0]["detail"], "the detail")
			self.assertEqual(events[0]["msg"], "a headline")

	def test_old_format_event_line_without_detail_loads(self):
		with tempfile.TemporaryDirectory() as tmp:
			day = time.strftime("%Y%m%d", time.localtime(1700000100.0))
			with open(os.path.join(tmp, "samples-%s.jsonl" % day), "w", encoding="utf-8") as f:
				f.write('{"t":1700000100,"e":{"level":"warn","kind":"alert","msg":"an old headline"}}\n')
			samples, events = state.History(tmp).load(0)
			self.assertEqual(len(events), 1)
			self.assertEqual(events[0]["msg"], "an old headline")
			self.assertNotIn("detail", events[0])  # no detail key: old lines still load and render

	def test_paged_is_info_during_load_grace_and_crit_after(self):
		alert, _ = self._one("paged", [_snap(llama=dict(_snap()["llama"], com=4600.0, age=10.0))])
		self.assertEqual(alert["level"], "info")  # within load_grace_s of the load
		alert, _ = self._one("paged", [_snap(llama=dict(_snap()["llama"], com=4600.0, age=1000.0))])
		self.assertEqual(alert["level"], "crit")  # still paged well after the load

	def test_spill_headline_and_detail_wording(self):
		alert, _ = self._one("spill", [_snap(llama=dict(_snap()["llama"], shr=1735.0, allow=525.0, over=1210.0, allow_known=True, allow_src="measured", floor=525.0, floor_known=True))])
		self.assertEqual(alert["msg"], "shared 1,735 MiB, 1,210 MiB beyond expected")
		self.assertIn("expected 525 MiB (measured with no VRAM pressure)", alert["detail"])
		self.assertNotIn("% of all shared", alert["detail"])
		alert_proj, _ = self._one("spill", [_snap(llama=dict(_snap()["llama"], shr=1735.0, allow=525.0, over=1210.0, allow_known=True, allow_src="projected", floor=525.0, floor_known=True))])
		self.assertEqual(alert_proj["msg"], "shared 1,735 MiB, 1,210 MiB beyond expected")
		self.assertIn("expected 525 MiB (projected by llama.cpp)", alert_proj["detail"])
		self.assertNotIn("% of all shared", alert_proj["detail"])

	def test_spill_is_info_when_only_excess_and_warn_when_grew(self):
		alert, _ = self._one("spill", [_snap(llama=dict(_snap()["llama"], shr=428.0, allow=128.0, over=300.0, allow_known=True))])
		self.assertEqual(alert["level"], "info")  # over the expected allowance, but not grown past the floor
		alert, _ = self._one("spill", [_snap(llama=dict(_snap()["llama"], shr=512.0, floor=128.0, floor_known=True))])
		self.assertEqual(alert["level"], "warn")  # grown past the resident minimum


class TestRing(unittest.TestCase):
	"""Tests for Ring 10 s and 60 s summary rollups and exactness against per-second reference."""

	@staticmethod
	def _legacy_cols(r, t_end, seconds, n, how="max"):
		"""Baseline cols implementation from parent commit."""
		start = int(t_end) + 1 - seconds
		out = []
		for j in range(n):
			a, b = start + seconds * j // n, start + seconds * (j + 1) // n
			xs = [x for x in map(r.at, range(a, max(b, a + 1))) if x == x]
			out.append(state.agg(xs, how) if xs else state.NAN)
		return out

	@staticmethod
	def _ref_cols(r, t_end, seconds, n, how):
		"""Reference implementation computing each column over bucket-aligned ranges."""
		ratio = seconds / n
		size = 60 if ratio >= 120 else 10 if ratio >= 20 else 0
		if not size:
			return TestRing._legacy_cols(r, t_end, seconds, n, how)
		b_end = int(t_end) // size
		n_buckets = seconds // size
		b_start = b_end + 1 - n_buckets
		out = []
		for j in range(n):
			bj_a = b_start + n_buckets * j // n
			bj_b = b_start + n_buckets * (j + 1) // n
			xs = [r.at(s) for s in range(bj_a * size, bj_b * size) if r.at(s) == r.at(s)]
			out.append(state.agg(xs, how) if xs else state.NAN)
		return out

	@staticmethod
	def _approx_eq(a, b, tol=1e-4):
		if math.isnan(a) and math.isnan(b):
			return True
		if math.isnan(a) or math.isnan(b):
			return False
		return abs(a - b) <= tol * (max(abs(a), abs(b)) + 1.0)

	def test_ring_random_data_24h_bucket_aligned(self):
		rng = random.Random(42)
		now = 1700000000
		for kind in ("max", "min", "mean", "last"):
			r = state.Ring(24 * 3600, how=kind)
			for s in range(now - 86400 + 1, now + 1):
				if rng.random() > 0.3:
					r.put(s, rng.uniform(0.0, 1000.0))
			for secs in (3600, 21600, 86400):
				res = r.cols(now, secs, 112, kind)
				ref = self._ref_cols(r, now, secs, 112, kind)
				self.assertEqual(len(res), 112)
				for j in range(112):
					self.assertTrue(
						self._approx_eq(res[j], ref[j]),
						"kind=%s secs=%d col=%d: got %s, ref %s" % (kind, secs, j, res[j], ref[j]),
					)

	def test_ring_short_windows_legacy_exact(self):
		rng = random.Random(42)
		now = 1700000000
		for kind in ("max", "min", "mean", "last"):
			r = state.Ring(24 * 3600, how=kind)
			for s in range(now - 1000, now + 1):
				if rng.random() > 0.2:
					r.put(s, float(s % 50))
			for secs in (300, 900):
				res = r.cols(now, secs, 112, kind)
				leg = self._legacy_cols(r, now, secs, 112, kind)
				self.assertEqual(len(res), 112)
				for j in range(112):
					self.assertTrue(
						self._approx_eq(res[j], leg[j]),
						"legacy match kind=%s secs=%d col=%d: got %s, leg %s" % (kind, secs, j, res[j], leg[j]),
					)

	def test_ring_overwrite_keeps_buckets_exact(self):
		now = 1700000000
		for kind in ("max", "min", "mean", "last"):
			r = state.Ring(24 * 3600, how=kind)
			base_s = now - 500
			for s in range(base_s, base_s + 120):
				r.put(s, 10.0)
			r.put(base_s + 5, 99.0)
			r.put(base_s + 65, 1.0)
			for size in (10, 60):
				b = (base_s + 5) // size
				xs = [r.at(s) for s in range(b * size, (b + 1) * size) if r.at(s) == r.at(s)]
				expected = state.agg(xs, kind)
				if size == 10:
					val = r.v10[b % r.span10] if kind != "mean" else (r.sum10[b % r.span10] / r.cnt10[b % r.span10])
				else:
					val = r.v60[b % r.span60] if kind != "mean" else (r.sum60[b % r.span60] / r.cnt60[b % r.span60])
				self.assertTrue(
					self._approx_eq(val, expected),
					"overwrite mismatch kind=%s size=%d: got %s, expected %s" % (kind, size, val, expected),
				)

	def test_ring_stale_buckets_read_empty(self):
		now = 1700000000
		r = state.Ring(24 * 3600, how="max")
		r.put(now, 42.0)
		far_future = now + 25 * 3600
		cols = r.cols(far_future, 3600, 112, "max")
		self.assertEqual(len(cols), 112)
		for c in cols:
			self.assertTrue(math.isnan(c), "expected NaN for stale bucket, got %s" % c)

	def test_ring_default_kind_works(self):
		r = state.Ring(3600)
		self.assertEqual(r.how, "max")
		now = 1700000000
		r.put(now, 5.0)
		cols = r.cols(now, 300, 10)
		self.assertEqual(len(cols), 10)
		self.assertEqual(cols[-1], 5.0)


CASES = [
	("router_mode_preset_loaded_and_flags_parsed", TestState("test_router_mode_preset_loaded_and_flags_parsed")),
	("single_model_mode_process_counted", TestState("test_single_model_mode_process_counted")),
	("router_flagless_loaded_model_no_crash_and_notice", TestState("test_router_flagless_loaded_model_no_crash_and_notice")),
	("single_model_process_not_found_notice_and_no_crash", TestState("test_single_model_process_not_found_notice_and_no_crash")),
	("slots_401_and_api_key_header", TestState("test_slots_401_and_api_key_header")),
	("uma_zero_vram_adapter", TestState("test_uma_zero_vram_adapter")),
	("high_page_in_rate_raises_no_alert", TestState("test_high_page_in_rate_raises_no_alert")),
	("invalid_state_dir_disables_persistence_with_notice", TestState("test_invalid_state_dir_disables_persistence_with_notice")),
	("log_dir_none_and_no_router_log_touches_no_folder", TestState("test_log_dir_none_and_no_router_log_touches_no_folder")),
	("threshold_arithmetic_8_16_64_gib", TestState("test_threshold_arithmetic_8_16_64_gib")),
	("discovery_gpu_selection_errors_and_multi_resident", TestState("test_discovery_gpu_selection_errors_and_multi_resident")),
	("models_401_discovery_and_auth_alert", TestState("test_models_401_discovery_and_auth_alert")),
	("multiple_independent_servers_and_single_model_pid_matching", TestState("test_multiple_independent_servers_and_single_model_pid_matching")),
	("every_alert_headline_fits_in_72_chars", TestAlertHeadlines("test_every_alert_headline_fits_in_72_chars")),
	("detail_carried_through_flag_and_emit", TestAlertHeadlines("test_detail_carried_through_flag_and_emit")),
	("detail_persisted_and_loaded", TestAlertHeadlines("test_detail_persisted_and_loaded")),
	("old_format_event_line_without_detail_loads", TestAlertHeadlines("test_old_format_event_line_without_detail_loads")),
	("paged_is_info_during_load_grace_and_crit_after", TestAlertHeadlines("test_paged_is_info_during_load_grace_and_crit_after")),
	("spill_headline_and_detail_wording", TestAlertHeadlines("test_spill_headline_and_detail_wording")),
	("spill_is_info_when_only_excess_and_warn_when_grew", TestAlertHeadlines("test_spill_is_info_when_only_excess_and_warn_when_grew")),
	("three_slots_only_slot_2_processing", TestState("test_three_slots_only_slot_2_processing")),
	("two_slots_processing_primary_is_newer", TestState("test_two_slots_processing_primary_is_newer")),
	("per_slot_rates_separate", TestState("test_per_slot_rates_separate")),
	("slots_reply_bad_types_and_empty", TestState("test_slots_reply_bad_types_and_empty")),
	("ring_random_data_24h_bucket_aligned", TestRing("test_ring_random_data_24h_bucket_aligned")),
	("ring_short_windows_legacy_exact", TestRing("test_ring_short_windows_legacy_exact")),
	("ring_overwrite_keeps_buckets_exact", TestRing("test_ring_overwrite_keeps_buckets_exact")),
	("ring_stale_buckets_read_empty", TestRing("test_ring_stale_buckets_read_empty")),
	("ring_default_kind_works", TestRing("test_ring_default_kind_works")),
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
