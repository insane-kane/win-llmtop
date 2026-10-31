#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Aaron Kane <software@kanenet.us>
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
import re
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llmtop_lib import log as logmod  # noqa: E402
from llmtop_lib import pages, state, win  # noqa: E402


class _StubServer:
	"""Ephemeral HTTP server on 127.0.0.1 for testing Collector."""

	def __init__(self, models_data, slots_data=None, require_key=None, slots_status=200, require_key_on_models=False, slots_error_body=None):
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
		self.slots_error_body = slots_error_body
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
						self._send_json(parent.slots_status, parent.slots_error_body or {"error": {"code": parent.slots_status}})
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

	def test_slots_400_not_loaded_is_normal_between_polls(self):
		# With autoload=false the router answers 400 "model is not loaded" when the preset
		# was unloaded between the /models poll and this one: a normal result, not a flag.
		models_data = [
			{
				"id": "preset-x",
				"object": "model",
				"status": {"value": "loaded", "args": ["llama-server.exe", "-m", "x.gguf"]},
			}
		]
		srv = _StubServer(
			models_data,
			slots_status=400,
			slots_error_body={"error": {"code": 400, "message": "model is not loaded"}},
		)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				snap = c.tick(1700000100.0)
				p = snap["presets"]["preset-x"]
				self.assertIsNone(p["slot_error"])
				self.assertEqual(p["status"], "loaded")
				self.assertNotIn("slots:preset-x", c.alerts.active)
				self.assertTrue(all("autoload=false" in path for path, _ in srv.requests if path.startswith("/slots")), srv.requests)

				srv.slots_error_body = {"error": {"code": 400}}  # a different 400 keeps its handling
				snap2 = c.tick(1700000104.0)  # beyond the 3 s slot cadence so the poll really runs
				self.assertEqual(snap2["presets"]["preset-x"]["slot_error"], "HTTP 400")
				self.assertIn("slots:preset-x", c.alerts.active)
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

	def test_amd_adapter_load_drives_gpu_series(self):
		models_data = [{"id": "m1", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "m1.gguf"]}}]
		srv = _StubServer(models_data)
		luid = "0x00000000_0x0001C3D4"
		adapters = [{"name": "Stub Discrete GPU", "vendor": win.AMD_VENDOR, "vram": 20480.0, "shared": 32768.0, "luid": luid, "budget": 19000.0, "budget_shared": 30000.0}]
		sample_data = {
			"procs": {2000: {"ded": 16000.0, "shr": 128.0, "com": 16128.0, "eng": {"Compute": 0.0}}},
			"procs_by_luid": {2000: {luid: {"ded": 16000.0, "shr": 128.0, "com": 16128.0, "eng": {"Compute": 0.0}}}},
			"adapters": {luid: {"ded": 16200.0, "shr": 200.0, "com": 16400.0}},
			"sys": {"pages_in": 0.0, "pages_out": 0.0, "standby": 8192.0, "free": 16384.0, "modified": 128.0, "pagefile_pct": 5.0},
		}
		stub = types.SimpleNamespace(name="Stub Discrete GPU", read=lambda: {"load": 98.0, "mhz": 2876.0})
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win(adapters=adapters, sample_data=sample_data), mock.patch.object(win, "AmdLoad", return_value=stub):
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				snap = c.tick(1700000100.0)
				self.assertEqual(snap["gpu"]["load"], 98.0)
				self.assertEqual(snap["gpu"]["mhz"], 2876.0)
				# the engine counters read 0 for compute work: the series takes the adapter's own figure instead
				self.assertEqual(c.series_values(snap)["gpu_llama"], 98.0)
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
				self.assertIn("--no-slots", c.alerts.active["slots:p1"]["detail"])
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

	def test_rate_survives_sample_where_only_n_prompt_tokens_grows(self):
		slots_state = [
			{"id": 0, "n_ctx": 8192, "id_task": 10, "is_processing": True, "n_prompt_tokens": 2048, "n_prompt_tokens_cache": 0, "n_prompt_tokens_processed": 0, "next_token": [{"n_decoded": 0, "n_remain": 100}]},
		]
		models_data = [{"id": "m1", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "m1.gguf"]}}]
		srv = _StubServer(models_data, slots_data=slots_state)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				# Sample 1 at t=100.0: done=0
				c.due.clear()
				c.tick(1700000100.0)

				# Sample 2 at t=102.0: done=2048 -> rate established
				slots_state[0]["n_prompt_tokens_processed"] = 2048
				c.due.clear()
				snap2 = c.tick(1700000102.0)
				rate1 = snap2["presets"]["m1"]["rate_pre"]
				self.assertAlmostEqual(rate1, 1024.0, delta=10.0)

				# Sample 3 at t=103.0: only n_prompt_tokens grows (to 4096), done stays 2048
				slots_state[0]["n_prompt_tokens"] = 4096
				c.due.clear()
				snap3 = c.tick(1700000103.0)
				rate2 = snap3["presets"]["m1"]["rate_pre"]
				self.assertFalse(math.isnan(rate2))
				self.assertGreater(rate2, 0.0)
				self.assertAlmostEqual(rate2, rate1, delta=10.0)
		finally:
			srv.close()

	def test_long_prefill_recorded_sequence_phase_and_progress(self):
		# Recorded llama-server b11669 /slots sequence (8 rows)
		recorded_rows = [
			(2048, 0, 0),
			(4096, 0, 2048),
			(6144, 0, 4096),
			(6144, 0, 6144),
			(8192, 0, 6144),
			(8192, 0, 8192),
			(10240, 0, 8192),
			(10240, 0, 10240),
		]
		slots_state = [
			{"id": 0, "n_ctx": 16384, "id_task": 101, "is_processing": True,
			 "n_prompt_tokens": 2048, "n_prompt_tokens_cache": 0, "n_prompt_tokens_processed": 0,
			 "next_token": [{"n_decoded": 0, "n_remain": 100}]},
		]
		models_data = [{"id": "m1", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "m1.gguf"]}}]
		srv = _StubServer(models_data, slots_data=slots_state)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				for step, (n_prompt, cached, done) in enumerate(recorded_rows):
					slots_state[0]["n_prompt_tokens"] = n_prompt
					slots_state[0]["n_prompt_tokens_cache"] = cached
					slots_state[0]["n_prompt_tokens_processed"] = done
					c.due.clear()
					t = 1700000100.0 + step * 4.2
					snap = c.tick(t)
					p = snap["presets"]["m1"]
					# Phase is prefill at every step
					self.assertEqual(p["phase"], "prefill", "step %d expected prefill, got %s" % (step, p["phase"]))
					# Rendered Live block never contains DECODE
					rendered_lines = pages.model_block(c, snap, pages.View(), "m1", p, 120, 2)
					rendered_text = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in rendered_lines)
					self.assertNotIn("DECODE", rendered_text, "step %d rendered DECODE" % step)
					# While total is unknown (step > 0 where n_prompt has grown), it shows tokens so far and no % or ETA
					if step > 0:
						self.assertTrue(p["slot"]["prompt_grown"], "step %d expected prompt_grown=True" % step)
						pre_line = re.sub(r"\x1b\[[0-9;]*m", "", rendered_lines[1])
						self.assertIn("tokens so far", pre_line, "step %d missing 'tokens so far': %s" % (step, pre_line))
						self.assertNotIn("%", pre_line, "step %d has '%%': %s" % (step, pre_line))
						self.assertNotIn("ETA", pre_line, "step %d has 'ETA': %s" % (step, pre_line))
		finally:
			srv.close()

	def test_long_prefill_stale_first_sample_never_draws_a_bar(self):
		# A 33,965-token prompt at 20 Hz on llama-server b11669: the task's first sample still holds the last task's 96,646
		recorded_rows = [
			(96646, 0, 0), (2048, 0, 0), (2048, 0, 2048), (4096, 0, 2048),
			(4096, 0, 4096), (6144, 0, 4096), (6144, 0, 6144),
		]
		slots_state = [
			{"id": 0, "n_ctx": 16384, "id_task": 100, "is_processing": False,
			 "n_prompt_tokens": 0, "n_prompt_tokens_cache": 0, "n_prompt_tokens_processed": 0,
			 "next_token": [{"n_decoded": 0, "n_remain": 100}]},
		]
		models_data = [{"id": "m1", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "m1.gguf"]}}]
		srv = _StubServer(models_data, slots_data=slots_state)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				c.due.clear()
				c.tick(1700000100.0)  # the previous task, idle: the first row below is a real task change
				slots_state[0]["id_task"] = 101
				slots_state[0]["is_processing"] = True
				for step, (n_prompt, cached, done) in enumerate(recorded_rows):
					slots_state[0]["n_prompt_tokens"] = n_prompt
					slots_state[0]["n_prompt_tokens_cache"] = cached
					slots_state[0]["n_prompt_tokens_processed"] = done
					c.due.clear()
					snap = c.tick(1700000100.0 + (step + 1) * 4.2)
					p = snap["presets"]["m1"]
					self.assertEqual(p["phase"], "prefill", "step %d expected prefill, got %s" % (step, p["phase"]))
					text = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in pages.model_block(c, snap, pages.View(), "m1", p, 120, 2)]
					self.assertNotIn("DECODE", "\n".join(text), "step %d rendered DECODE" % step)
					if step > 0:  # the stale first sample is gone: the total is unknown, so no bar and no percentage
						self.assertTrue(p["slot"]["prompt_grown"], "step %d expected prompt_grown=True" % step)
						self.assertIn("tokens so far", text[1], "step %d: %s" % (step, text[1]))
						self.assertNotIn("%", text[1], "step %d: %s" % (step, text[1]))
						self.assertNotIn("ETA", text[1], "step %d: %s" % (step, text[1]))
		finally:
			srv.close()

	def test_damaged_history_lines_do_not_stop_the_start(self):
		now = int(time.time())
		good_event = {"t": now, "e": {"level": "info", "kind": "alert", "msg": "m"}}
		with tempfile.TemporaryDirectory() as td:
			with open(os.path.join(td, "samples-%s.jsonl" % time.strftime("%Y%m%d")), "w", encoding="utf-8") as f:
				for line in ("12", "[1]", '{"t":"x","v":{}}', '{"t":%d,"v":[1]}' % now, '{"t":%d,"e":{"level":"info"}}' % now, '{"t":1e999,"v":{"cpu":1}}',
					'{"t":%d,"e":{"level":"ERROR","kind":"log","msg":"x"}}' % now, json.dumps({"t": now, "v": {"cpu": 12.5, "ram": None, "free": "x"}}), json.dumps(good_event)):
					f.write(line + "\n")
			samples, events = state.History(td).load(now - 60)
			self.assertEqual(samples, [(now, {"cpu": 12.5})])
			self.assertEqual([e["msg"] for e in events], ["m"])
			with open(os.path.join(td, "requests.jsonl"), "w", encoding="utf-8") as f:
				f.write('12\n{"wall":1}\n{"preset":"a","task":1,"wall":2}\n')
			self.assertEqual(state.RequestLog(td).load(), [{"wall": 1}, {"preset": "a", "task": 1, "wall": 2}])
			with _patch_win():
				c = state.Collector(dict(state.CFG, router="http://127.0.0.1:9", state_dir=td, log_dir=None, persist=False, calibrate=False))
				c.poll_models = lambda now: None
				c.load_history()
			self.assertEqual([r["preset"] for r in c.requests], ["a"])
			self.assertEqual(c.rings["cpu"].at(now), 12.5)

	def test_prompt_total_narrows_so_the_bar_never_steps_back(self):
		# real figures: 21,971 tokens cached, 43,807 in all; one estimate per line wobbled the bar 29 -> 28, 57 -> 56, 85 -> 84
		cached, whole, r, last = 21971, 43807, {}, -1.0
		for n in range(2057, 21836, 2048):
			est = state.Collector.prompt_total(r, (n, round((n + cached) / whole, 2)), cached)
			pct = 100 * n / (est - cached)
			self.assertGreater(pct, last, "n=%d estimate %d" % (n, est))
			last = pct
		self.assertLess(abs(est - whole) / whole, 0.005, "estimate %d" % est)
		self.assertIsNone(state.Collector.prompt_total({}, None, cached))
		self.assertIsNone(state.Collector.prompt_total({}, (0, 0.0), cached))

	def test_long_prefill_bar_from_the_log_never_goes_back(self):
		recorded_rows = [
			(96646, 0, 0), (2048, 0, 0), (2048, 0, 2048), (4096, 0, 2048),
			(4096, 0, 4096), (6144, 0, 4096), (6144, 0, 6144),
		]
		total_prompt = 33965  # what the server's log works the prompt out to, cached prefix included

		class _StubTail:
			path = "stub.log"

			def poll(self):
				return []

		slots_state = [
			{"id": 0, "n_ctx": 65536, "id_task": 100, "is_processing": False,
			 "n_prompt_tokens": 0, "n_prompt_tokens_cache": 0, "n_prompt_tokens_processed": 0,
			 "next_token": [{"n_decoded": 0, "n_remain": 100}]},
		]
		models_data = [{"id": "m1", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "m1.gguf"]}}]
		srv = _StubServer(models_data, slots_data=slots_state)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				asm = logmod.Assembler("m1")
				c.follow["m1"] = (_StubTail(), asm)
				c.due.clear()
				c.tick(1700000100.0)
				slots_state[0]["id_task"] = 101
				slots_state[0]["is_processing"] = True
				last_pct, last_snap, last_p = -1, None, None
				for step, (n_prompt, cached, done) in enumerate(recorded_rows):
					slots_state[0]["n_prompt_tokens"] = n_prompt
					slots_state[0]["n_prompt_tokens_cache"] = cached
					slots_state[0]["n_prompt_tokens_processed"] = done
					if done > 0:  # the server's line for the batch that finished: n_tokens and the share of the prompt it leaves
						asm.progress[101] = (done, done / total_prompt)
					c.due.clear()
					snap = c.tick(1700000100.0 + (step + 1) * 4.2)
					p = snap["presets"]["m1"]
					text = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in pages.model_block(c, snap, pages.View(), "m1", p, 120, 2)]
					line = text[1]
					self.assertEqual(p["phase"], "prefill", "step %d expected prefill, got %s" % (step, p["phase"]))
					self.assertNotIn("DECODE", "\n".join(text), "step %d rendered DECODE" % step)
					self.assertIn("PREFILL", line, "step %d: %s" % (step, line))
					m = re.search(r"(\d+)%", line) if step > 0 else None  # step 0 is the stale first sample, before any batch line
					if m:
						pct = int(m.group(1))
						self.assertGreaterEqual(pct, last_pct, "step %d: the bar went back: %s" % (step, line))
						last_pct = pct
						self.assertIn("~", line, "step %d: the total is from the log and marked as such: %s" % (step, line))
					if p["rate_pre"] == p["rate_pre"]:  # a rate is known, so the ETA must be a number too
						self.assertEqual(p["eta"], p["eta"], "step %d: no ETA with a rate: %s" % (step, line))
					last_snap, last_p = snap, p
				self.assertGreater(last_pct, 0, "the bar never showed a percentage: %s" % last_p)
				# the bar case survives each Live width; the model block is not clipped, so ETA is there when a rate is
				for w in (80, 120, 200):
					text = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in pages.model_block(c, last_snap, pages.View(), "m1", last_p, w, 2)]
					pre = next(ln for ln in text if "PREFILL" in ln)
					self.assertIn("%", pre, (w, pre))
					self.assertIn("ETA", pre, (w, pre))
					for ln in pages.render(c, last_snap, pages.View(), w, 40):
						self.assertLessEqual(pages.vlen(ln), w, (w, ln))  # rendered pages are clipped to the width
		finally:
			srv.close()

	def test_busy_slot_stays_on_screen(self):
		slots_state = [
			{"id": 0, "n_ctx": 8192, "id_task": 1, "is_processing": True, "n_prompt_tokens": 4096,
			 "n_prompt_tokens_cache": 0, "n_prompt_tokens_processed": 2048, "next_token": [{"n_decoded": 0, "n_remain": 100}]},
			{"id": 1, "n_ctx": 8192, "id_task": 0, "is_processing": False, "n_prompt_tokens": 0,
			 "n_prompt_tokens_cache": 0, "n_prompt_tokens_processed": 0, "next_token": [{"n_decoded": 0, "n_remain": 0}]},
		]
		models_data = [{"id": "m1", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "m1.gguf"]}}]
		srv = _StubServer(models_data, slots_data=slots_state)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				c.due.clear()
				snap1 = c.tick(1700000100.0)  # slot 0 prefilling alone
				self.assertEqual(snap1["presets"]["m1"]["slot"]["id"], 0)
				slots_state[1].update(id_task=2, is_processing=True, n_prompt_tokens=2048, n_prompt_tokens_cache=0,
					n_prompt_tokens_processed=2048, next_token=[{"n_decoded": 5, "n_remain": 95}])
				c.due.clear()
				p2 = c.tick(1700000101.0)["presets"]["m1"]
				self.assertEqual(p2["slot"]["id"], 0, "the page swapped to the newer request on another slot")
				self.assertEqual(p2["phase"], "prefill", "PREFILL and DECODE swapped: %s" % p2["phase"])
				slots_state[0].update(is_processing=False)
				c.due.clear()
				p3 = c.tick(1700000102.0)["presets"]["m1"]
				self.assertEqual(p3["slot"]["id"], 1, "the shown slot did not move on once the old one went idle")
				self.assertEqual(p3["phase"], "decode")
		finally:
			srv.close()

	def test_decode_context_line_counts_generated_tokens_once(self):
		slots_state = [
			{"id": 0, "n_ctx": 65536, "id_task": 5, "is_processing": True, "n_prompt_tokens": 36510,
			 "n_prompt_tokens_cache": 36478, "n_prompt_tokens_processed": 27, "next_token": [{"n_decoded": 5, "n_remain": 95}]},
		]
		models_data = [{"id": "m1", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "m1.gguf"]}}]
		srv = _StubServer(models_data, slots_data=slots_state)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				c.due.clear()
				snap = c.tick(1700000100.0)
				p = snap["presets"]["m1"]
				self.assertEqual(p["phase"], "decode")
				text = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in pages.model_block(c, snap, pages.View(), "m1", p, 120, 2))
				self.assertIn("prompt 36,505 = 36,478 cached", text)  # cached prefix plus the 27 new tokens, not the 36,510 queued with 5 generated
				self.assertIn("+ 27 new", text)
		finally:
			srv.close()

	def test_collector_stale_sample_updated_seconds_ago(self):
		slots_state = [
			{"id": 0, "n_ctx": 8192, "id_task": 10, "is_processing": True, "n_prompt_tokens": 2048, "n_prompt_tokens_cache": 0, "n_prompt_tokens_processed": 1024, "next_token": [{"n_decoded": 0, "n_remain": 100}]},
		]
		models_data = [{"id": "m1", "object": "model", "status": {"value": "loaded", "args": ["llama-server.exe", "-m", "m1.gguf"]}}]
		srv = _StubServer(models_data, slots_data=slots_state)
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win():
				cfg = dict(state.CFG, router=srv.url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda preset: None
				# Good sample at t=100.0
				c.due.clear()
				snap1 = c.tick(1700000100.0)
				p1 = snap1["presets"]["m1"]
				lines1 = pages.model_block(c, snap1, pages.View(), "m1", p1, 120, 2)
				self.assertNotIn("updated", re.sub(r"\x1b\[[0-9;]*m", "", lines1[1]))

				# Next poll times out (simulate OSError) at t=103.5
				orig_get = c.get
				def fail_get(path):
					if "/slots" in path:
						raise OSError("timed out")
					return orig_get(path)
				c.get = fail_get
				c.due.clear()
				snap2 = c.tick(1700000103.5)
				p2 = snap2["presets"]["m1"]
				lines2 = pages.model_block(c, snap2, pages.View(), "m1", p2, 120, 2)
				plain2 = re.sub(r"\x1b\[[0-9;]*m", "", lines2[1])
				self.assertTrue(plain2.endswith("· updated 3s ago"), plain2)
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

	def test_request_log_trimmed_only_when_persisting(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = os.path.join(tmp, "requests.jsonl")
			with open(path, "w", encoding="utf-8") as f:
				for i in range(25):
					f.write(json.dumps({"n": i}) + "\n")
			kept = state.RequestLog(tmp, persist=False).load(limit=10)
			self.assertEqual([r["n"] for r in kept], list(range(15, 25)))  # the last 10 either way
			with open(path, encoding="utf-8") as f:
				self.assertEqual(sum(1 for _ in f), 25)  # no persist: the file is untouched
			kept = state.RequestLog(tmp, persist=True).load(limit=10)
			self.assertEqual([r["n"] for r in kept], list(range(15, 25)))
			with open(path, encoding="utf-8") as f:
				self.assertEqual([json.loads(line)["n"] for line in f], list(range(15, 25)))  # trimmed to the last 10
			self.assertFalse(os.path.exists(path + ".tmp"))  # no temp file left behind

	def test_requests_in_memory_capped(self):
		obj = types.SimpleNamespace(requests=[], req_seq=0, cfg={"persist": False}, reqlog=None)
		for i in range(60001):
			state.Collector.finish_request(obj, {"preset": "p", "ms": 0}, 1700000000.0 + i)
		self.assertEqual(len(obj.requests), 50000)
		self.assertEqual(obj.requests[0]["wall"], 1700000000.0 + 10001)  # the oldest dropped, the newest kept
		self.assertEqual(obj.requests[-1]["wall"], 1700000000.0 + 60000)
		self.assertGreater(obj.req_seq, 0)  # the counter changed

	def test_calibration_write_is_atomic(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = os.path.join(tmp, "calibration.json")
			first = {"abc123def456": {"v": 4, "overhead": 1234, "at": 1700000100.0, "preset": "m1"}}
			state.write_json_atomic(path, first, indent=1)
			with open(path, encoding="utf-8") as f:
				self.assertEqual(json.load(f), first)

			def broken_dump(obj, f, indent=None):
				f.write('{"par')  # a few characters, then the write fails
				raise OSError("disk full")

			with mock.patch.object(state.json, "dump", side_effect=broken_dump):
				self.assertRaises(OSError, state.write_json_atomic, path, {"new": 1})
			with open(path, encoding="utf-8") as f:
				self.assertEqual(json.load(f), first)  # the old content survives a failed write
			self.assertFalse(os.path.exists(path + ".tmp"))  # no temp file left behind
			state.write_json_atomic(path, {"new": 1}, indent=1)
			with open(path, encoding="utf-8") as f:
				self.assertEqual(json.load(f), {"new": 1})  # a normal write replaces the content

	def test_calibration_load_drops_spilled_and_negative(self):
		with tempfile.TemporaryDirectory() as tmp, _patch_win():
			state_dir = os.path.join(tmp, "state")
			os.makedirs(state_dir, exist_ok=True)
			calib_data = {
				"good": {"v": 4, "overhead": 250, "at": 1700000100.0, "preset": "m_good", "spilled": False, "idle_shared": 100},
				"negative": {"v": 4, "overhead": -575, "at": 1700000100.0, "preset": "m_neg", "spilled": False, "idle_shared": 200},
				"spilled": {"v": 4, "overhead": 400, "at": 1700000100.0, "preset": "m_spill", "spilled": True, "idle_shared": 370},
			}
			with open(os.path.join(state_dir, "calibration.json"), "w", encoding="utf-8") as f:
				json.dump(calib_data, f)
			cfg = dict(state.CFG, state_dir=state_dir, persist=False, calibrate=False)
			c = state.Collector(cfg)
			self.assertEqual(set(c.calib.keys()), {"good", "negative", "spilled"})
			# "good" keeps overhead, spilled flag, and idle_shared
			self.assertEqual(c.calib["good"]["overhead"], 250)
			self.assertFalse(c.calib["good"]["spilled"])
			self.assertEqual(c.calib["good"]["idle_shared"], 100)
			# "negative" loses overhead and spilled flag, keeps idle_shared
			self.assertNotIn("overhead", c.calib["negative"])
			self.assertNotIn("spilled", c.calib["negative"])
			self.assertEqual(c.calib["negative"]["idle_shared"], 200)
			# "spilled" loses overhead and spilled flag, keeps idle_shared
			self.assertNotIn("overhead", c.calib["spilled"])
			self.assertNotIn("spilled", c.calib["spilled"])
			self.assertEqual(c.calib["spilled"]["idle_shared"], 370)

	def test_forget_presets_removes_requests_and_calibration(self):
		with tempfile.TemporaryDirectory() as tmp:
			state_dir = os.path.join(tmp, "state")
			os.makedirs(state_dir)
			req_path = os.path.join(state_dir, "requests.jsonl")
			cal_path = os.path.join(state_dir, "calibration.json")
			with open(req_path, "w", encoding="utf-8") as f:
				f.write('{"preset": "x", "pn": 100, "wall": 1700000000.0}\n')
				f.write('{"preset": "y", "pn": 200, "wall": 1700000100.0}\n')
				f.write("not json at all\n")
				f.write('{"preset": "x", "pn": 300, "wall": 1700000200.0}\n')
				f.write('{"preset": "z", "pn": 400, "wall": 1700000300.0}\n')
			calib = {
				"kx": {"v": 4, "overhead": 100, "at": 1700000100.0, "preset": "x"},
				"kz": {"v": 4, "overhead": 300, "at": 1700000100.0, "preset": "z"},
				"k3": {"v": 4, "idle_shared": 30},  # no preset field
			}
			state.write_json_atomic(cal_path, calib, indent=1)

			counts = state.forget_presets(state_dir, ["x", "y"])
			self.assertEqual(counts, {"x": (2, 1), "y": (1, 0)})
			with open(req_path, encoding="utf-8") as f:
				kept = [json.loads(ln) for ln in f if ln.strip()]
			self.assertEqual([r["preset"] for r in kept], ["z"])  # the other preset survives, the corrupt line is dropped
			with open(cal_path, encoding="utf-8") as f:
				self.assertEqual(set(json.load(f)), {"kz", "k3"})
			self.assertFalse(os.path.exists(req_path + ".tmp"))

			# nothing stored for the name: the files are left as they are
			self.assertEqual(state.forget_presets(state_dir, ["nope"]), {"nope": (0, 0)})
			with open(req_path, encoding="utf-8") as f:
				self.assertEqual([json.loads(ln)["preset"] for ln in f if ln.strip()], ["z"])
			with open(cal_path, encoding="utf-8") as f:
				self.assertEqual(set(json.load(f)), {"kz", "k3"})

			# a state folder that does not exist is not created
			absent = os.path.join(tmp, "absent")
			self.assertEqual(state.forget_presets(absent, ["x"]), {"x": (0, 0)})
			self.assertFalse(os.path.exists(absent))

	def test_calibration_sample_gates(self):
		with tempfile.TemporaryDirectory() as tmp, _patch_win():
			cfg = dict(state.CFG, state_dir=os.path.join(tmp, "state"), persist=False, calibrate=False, load_grace_s=15.0, spill_mib=256.0, idle_headroom_mib=1024.0)
			c = state.Collector(cfg)
			fake_map = {
				"groups": {"attention": {"gpu": 1000.0, "cpu": 0.0}},
				"kv": {"layers": {"full": 1, "swa": 0}, "gpu": 500.0, "mtp": 0.0, "per_token": 1024},
				"ctx": 2048, "cache_k": "q8_0", "cache_v": "q8_0",
				"rec": {"total": 0.0}, "mtp_mib": 0.0,
				"draft": None, "fit": None,
			}
			# rows_total for fake_map is 1500.0
			c.mem = {"test_model": {"key": "test_model_key", "state": "ready", "map": fake_map}}
			k = c.calib_key("test_model_key")

			base_snap = {
				"t": 1700000100.0,
				"presets": {"test_model": {"status": "loaded"}},
				"gpu": None,
				"llama": {
					"present": True,
					"pids": [1234],
					"age": 20.0,
					"ded": 1600.0,
					"shr": 100.0,
					"over": 0.0,
				},
			}

			# 1. Sample while spilled is ignored
			snap_spilled = dict(base_snap, llama=dict(base_snap["llama"], over=300.0))
			c.calibrate(snap_spilled)
			self.assertNotIn(k, c.calib)

			# 2. Sample before load_grace_s is ignored
			snap_early = dict(base_snap, llama=dict(base_snap["llama"], age=5.0))
			c.calibrate(snap_early)
			self.assertNotIn(k, c.calib)

			# 3. Sample at 95% of projection with headroom: idle_shared is learned, overhead is not
			gpu_headroom = {"budget": 16384.0}
			snap_95 = dict(base_snap, gpu=gpu_headroom, llama=dict(base_snap["llama"], ded=1425.0, shr=200.0))
			c.calibrate(snap_95)
			self.assertIn(k, c.calib)
			self.assertNotIn("overhead", c.calib[k])
			self.assertEqual(c.calib[k].get("idle_shared"), 200)

			# 4. Settled, fully resident, unspilled sample: overhead is learned, idle_shared retained
			snap_good = dict(base_snap)
			c.calibrate(snap_good)
			self.assertIn(k, c.calib)
			self.assertEqual(c.calib[k]["overhead"], 100)
			self.assertFalse(c.calib[k]["spilled"])
			self.assertEqual(c.calib[k]["idle_shared"], 200)

	def test_fit_projection_cached_per_configuration(self):
		with tempfile.TemporaryDirectory() as tmp, _patch_win():
			bin_dir = os.path.join(tmp, "bin")
			os.mkdir(bin_dir)
			exe = os.path.join(bin_dir, "llama-server.exe")
			with open(exe, "wb") as f:
				f.write(b"fake")
			fit_exe = os.path.join(bin_dir, "llama-fit-params.exe")
			with open(fit_exe, "wb") as f:
				f.write(b"fake")
			model_path = os.path.join(tmp, "model.gguf")
			with open(model_path, "wb") as f:
				f.write(b"fake model")
			cfg = dict(state.CFG, state_dir=os.path.join(tmp, "state"), persist=False, calibrate=False)
			c = state.Collector(cfg)
			c.watched_exe = lambda: exe
			calls = {"n": 0}

			def fake_fit(server_exe, flags, timeout=60):
				calls["n"] += 1
				return {"gpu": 1000, "host": 100}

			with mock.patch.object(state.model, "fit_projection", side_effect=fake_fit), \
					mock.patch.object(state.model, "analyze", return_value={}), \
					mock.patch.object(state.model, "server_defaults", return_value={}):
				flags = {"model": model_path, "ctx-size": "8192", "n-gpu-layers": "99"}
				c.map_job("p", dict(flags), "k1")
				self.assertEqual(calls["n"], 1)
				c.map_job("p", dict(flags), "k1")  # the same configuration: served from the in-memory cache
				self.assertEqual(calls["n"], 1)
				flags["ctx-size"] = "16384"  # a different configuration: reruns
				c.map_job("p", dict(flags), "k2")
				self.assertEqual(calls["n"], 2)
				st = os.stat(model_path)
				os.utime(model_path, (st.st_atime, st.st_mtime + 100))  # a newer model file: reruns
				c.map_job("p", dict(flags), "k3")
				self.assertEqual(calls["n"], 3)

	def test_fit_cache_persisted_and_reloaded(self):
		with tempfile.TemporaryDirectory() as tmp, _patch_win():
			bin_dir = os.path.join(tmp, "bin")
			os.mkdir(bin_dir)
			exe = os.path.join(bin_dir, "llama-server.exe")
			with open(exe, "wb") as f:
				f.write(b"fake")
			fit_exe = os.path.join(bin_dir, "llama-fit-params.exe")
			with open(fit_exe, "wb") as f:
				f.write(b"fake")
			model_path = os.path.join(tmp, "model.gguf")
			with open(model_path, "wb") as f:
				f.write(b"fake model")
			flags = {"model": model_path, "ctx-size": "8192", "n-gpu-layers": "99"}
			calls = {"n": 0}

			def fake_fit(server_exe, flags, timeout=60):
				calls["n"] += 1
				return {"gpu": 1000, "host": 100}

			def build(state_dir, calibrate):
				os.makedirs(state_dir, exist_ok=True)
				c = state.Collector(dict(state.CFG, state_dir=state_dir, persist=False, calibrate=calibrate))
				c.watched_exe = lambda: exe
				return c

			with mock.patch.object(state.model, "fit_projection", side_effect=fake_fit), \
					mock.patch.object(state.model, "analyze", return_value={}), \
					mock.patch.object(state.model, "server_defaults", return_value={}):
				state1 = os.path.join(tmp, "state1")
				c1 = build(state1, True)
				c1.map_job("p", dict(flags), "k1")
				self.assertEqual(calls["n"], 1)
				self.assertTrue(os.path.isfile(os.path.join(state1, "fit-cache.json")))  # persisted with calibrate on
				c2 = build(state1, True)
				self.assertTrue(c2.fit_cache)  # the new Collector loads it from disk
				c2.map_job("p", dict(flags), "k1")
				self.assertEqual(calls["n"], 1)  # the same configuration is served from the reloaded cache
				state2 = os.path.join(tmp, "state2")
				c3 = build(state2, False)
				c3.map_job("p", dict(flags), "k1")
				self.assertEqual(calls["n"], 2)  # a fresh folder: computed once
				self.assertFalse(os.path.isfile(os.path.join(state2, "fit-cache.json")))  # calibrate off: nothing written

	def test_fit_params_policy(self):
		with tempfile.TemporaryDirectory() as tmp, _patch_win():
			bin_dir = os.path.join(tmp, "bin")
			os.mkdir(bin_dir)
			exe = os.path.join(bin_dir, "llama-server.exe")
			with open(exe, "wb") as f:
				f.write(b"fake")
			fit_exe = os.path.join(bin_dir, "llama-fit-params.exe")
			with open(fit_exe, "wb") as f:
				f.write(b"fake")
			model_path = os.path.join(tmp, "model.gguf")
			with open(model_path, "wb") as f:
				f.write(b"fake model")
			flags = {"model": model_path, "ctx-size": "8192", "n-gpu-layers": "99"}
			calls = {"n": 0}

			def fake_fit(server_exe, flags, timeout=60):
				calls["n"] += 1
				return {"gpu": 1000, "host": 100}

			def build(**over):
				c = state.Collector(dict(state.CFG, state_dir=os.path.join(tmp, "state"), persist=False, calibrate=False, **over))
				c.watched_exe = lambda: exe
				return c

			with mock.patch.object(state.model, "fit_projection", side_effect=fake_fit), \
					mock.patch.object(state.model, "analyze", return_value={}), \
					mock.patch.object(state.model, "server_defaults", return_value={}):
				c = build(fit_params="never")
				c.map_job("p", dict(flags), "k1")
				self.assertEqual(calls["n"], 0)  # never: not run
				self.assertIn("never", c.mem["p"]["fit_skipped"])
				c = build()
				c.last = {"gpu": {"budget": 10000.0, "ded": 9900.0}}  # 100 MiB of headroom, under the 256 MiB threshold
				c.map_job("p", dict(flags), "k1")
				self.assertEqual(calls["n"], 0)  # auto near the budget: skipped
				self.assertIn("skipped", c.mem["p"]["fit_skipped"])
				self.assertIsNotNone(c.mem["p"]["retry_at"])
				c = build(plan=True)
				c.last = {"gpu": {"budget": 10000.0, "ded": 9900.0}}
				c.map_job("p", dict(flags), "k1")
				self.assertEqual(calls["n"], 1)  # --plan: an explicit request, run regardless of headroom
				self.assertIsNone(c.mem["p"]["fit_skipped"])
				calls["n"] = 0
				c = build()
				c.last = {"gpu": {"budget": 10000.0, "ded": 5000.0}}  # 5 GiB of headroom
				c.map_job("p", dict(flags), "k1")
				self.assertEqual(calls["n"], 1)  # auto with room: run
				self.assertIsNone(c.mem["p"]["fit_skipped"])

	def test_models_backoff_connection_error_and_reset(self):
		"""When /models fails with connection error or timeout, poll interval backs off: 5s, 10s, 20s, 30s, 30s."""
		import urllib.error
		c = state.Collector(cfg=dict(state.CFG, router="http://127.0.0.1:8080", persist=False))

		polls = []
		def fail_conn(path, **k):
			polls.append(now[0])
			raise urllib.error.URLError("connection refused")

		c.get = fail_conn
		now = [1000.0]

		dummy_sock = mock.MagicMock()
		with mock.patch("socket.create_connection", return_value=dummy_sock):
			# 1st failure: backoff is 5s (due at 1005.0)
			c.tick(now[0])
			self.assertEqual(len(polls), 1)
			self.assertEqual(c.due["models"], 1005.0)

			# Intermediate tick before due: does not poll
			now[0] = 1002.0
			c.tick(now[0])
			self.assertEqual(len(polls), 1)

			# 2nd failure at 1005.0: backoff is 10s (due at 1015.0)
			def fail_timeout(path, **k):
				polls.append(now[0])
				raise TimeoutError("timed out")
			c.get = fail_timeout
			now[0] = 1005.0
			c.tick(now[0])
			self.assertEqual(len(polls), 2)
			self.assertEqual(c.due["models"], 1015.0)

			# 3rd failure at 1015.0: backoff is 20s (due at 1035.0)
			now[0] = 1015.0
			c.tick(now[0])
			self.assertEqual(len(polls), 3)
			self.assertEqual(c.due["models"], 1035.0)

			# 4th failure at 1035.0: backoff caps at 30s (due at 1065.0)
			now[0] = 1035.0
			c.tick(now[0])
			self.assertEqual(len(polls), 4)
			self.assertEqual(c.due["models"], 1065.0)

			# 5th failure at 1065.0: backoff stays 30s (due at 1095.0)
			now[0] = 1065.0
			c.tick(now[0])
			self.assertEqual(len(polls), 5)
			self.assertEqual(c.due["models"], 1095.0)

			# Success at 1095.0: resets backoff to normal 1s (due at 1096.0)
			c.get = lambda path, **k: (polls.append(now[0]), {"data": []})[1]
			now[0] = 1095.0
			c.tick(now[0])
			self.assertEqual(len(polls), 6)
			self.assertEqual(c._models_backoff, 0)
			self.assertEqual(c.due["models"], 1096.0)

			# HTTPError 401 at 1096.0: does not back off (next poll in 1s, due at 1097.0)
			def fail_401(path, **k):
				polls.append(now[0])
				raise urllib.error.HTTPError("http://127.0.0.1:8080/models", 401, "Unauthorized", {}, None)
			c.get = fail_401
			now[0] = 1096.0
			c.tick(now[0])
			self.assertEqual(len(polls), 7)
			self.assertEqual(c._models_backoff, 0)
			self.assertEqual(c.due["models"], 1097.0)

			# Another connection failure initiates backoff again
			c.get = fail_conn
			now[0] = 1097.0
			c.tick(now[0])
			self.assertEqual(len(polls), 8)
			self.assertEqual(c.due["models"], 1102.0)

			# Changing API key resets backoff and forces immediate poll
			c.cfg["api_key"] = "secret"
			now[0] = 1098.0  # Before 1102.0
			c.tick(now[0])
			self.assertEqual(len(polls), 9)  # Polled immediately!
			self.assertEqual(c.due["models"], 1103.0)  # Starts fresh 5s backoff

	def test_opener_for_caching_and_rebuild(self):
		"""Two calls for a loopback URL return the same opener; a remote one is rebuilt after 60 s."""
		op1 = state.opener_for("http://127.0.0.1:8080/models")
		op2 = state.opener_for("http://127.0.0.1:8080/models")
		self.assertIs(op1, op2)

		remote_url = "http://192.168.1.100:8080/models"
		cur_time = 1000000.0
		with mock.patch("time.time", side_effect=lambda: cur_time):
			r_op1 = state.opener_for(remote_url)
			cur_time += 30.0
			r_op2 = state.opener_for(remote_url)
			self.assertIs(r_op1, r_op2)
			cur_time += 31.0
			r_op3 = state.opener_for(remote_url)
			self.assertIsNot(r_op1, r_op3)

	def test_history_load_skips_old_day_file_unopened(self):
		"""History.load skips samples-YYYYMMDD.jsonl when its day ends at or before since; unparsable names read as today."""
		with tempfile.TemporaryDirectory() as td:
			now = time.time()
			today_str = time.strftime("%Y%m%d", time.localtime(now))
			old_time = now - 5 * 86400
			old_str = time.strftime("%Y%m%d", time.localtime(old_time))

			old_file = os.path.join(td, "samples-%s.jsonl" % old_str)
			cur_file = os.path.join(td, "samples-%s.jsonl" % today_str)

			cur_sec = int(now) - 60
			with open(cur_file, "w", encoding="utf-8") as f:
				f.write(json.dumps({"t": cur_sec, "v": {"cpu": 12.5}}) + "\n")

			# Old file is corrupt/unreadable JSON
			with open(old_file, "w", encoding="utf-8") as f:
				f.write("CORRUPT NOT JSON\n")

			hist = state.History(td)
			since = now - 86400

			opened = []
			real_open = open
			def tracking_open(path, *args, **kwargs):
				opened.append(os.path.normpath(str(path)))
				return real_open(path, *args, **kwargs)

			with mock.patch("builtins.open", side_effect=tracking_open):
				samples, events = hist.load(since)

			self.assertNotIn(os.path.normpath(old_file), opened)
			self.assertIn(os.path.normpath(cur_file), opened)
			self.assertEqual(len(samples), 1)
			self.assertEqual(samples[0][0], cur_sec)
			self.assertEqual(samples[0][1], {"cpu": 12.5})

			# An unparsable name is read as today (opened)
			bad_name = os.path.join(td, "samples-nondate.jsonl")
			with open(bad_name, "w", encoding="utf-8") as f:
				f.write(json.dumps({"t": cur_sec, "v": {"ram": 10.0}}) + "\n")

			opened.clear()
			with mock.patch("builtins.open", side_effect=tracking_open):
				samples, events = hist.load(since)

			self.assertNotIn(os.path.normpath(old_file), opened)
			self.assertIn(os.path.normpath(cur_file), opened)
			self.assertIn(os.path.normpath(bad_name), opened)
			self.assertEqual(len(samples), 2)

	def test_down_loopback_server_fails_fast(self):
		"""A closed local port fails within 0.4 s and applies back-off without making urllib request."""
		s = socket.socket()
		s.bind(("127.0.0.1", 0))
		closed_port = s.getsockname()[1]
		s.close()

		cfg = dict(state.CFG, router="http://127.0.0.1:%d" % closed_port, persist=False)
		c = state.Collector(cfg=cfg)
		self.assertFalse(c.router_up)

		t0 = time.monotonic()
		c.poll_models(time.time())
		elapsed = time.monotonic() - t0

		self.assertFalse(c.router_up)
		self.assertLess(elapsed, 0.4, "down loopback probe took %.4f s (expected < 0.4 s)" % elapsed)
		self.assertEqual(c._models_backoff, 5)


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
		"""Per-second reference over the same fixed slices: n slices of `seconds`/n ending at t_end, each a whole second at least."""
		first = int(t_end) * n // seconds - n + 1  # the first of the n fixed slices ending at t_end
		out = []
		for k in range(first, first + n):
			a, b = -(-k * seconds // n), -(-(k + 1) * seconds // n)
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
		k_end = b_end * n // n_buckets  # the bucket-aligned slice that holds t_end
		edge = lambda k: -(-k * n_buckets // n)
		out = []
		for k in range(k_end - n + 1, k_end + 1):
			bj_a, bj_b = edge(k), edge(k + 1)
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

	def test_ring_cols_are_fixed_slices(self):
		"""A chart scrolls: the spike's column count and index move at most one per second, never re-slicing the window."""
		base = 1700000000
		S = base + 500
		r = state.Ring(24 * 3600, how="max")
		for s in range(base, base + 1000):
			r.put(s, 0.0)
		r.put(S, 100.0)
		prev_idx = None
		counts = set()
		for t_end in range(S + 20, S + 61):
			cols = r.cols(t_end, 300, 112)
			self.assertEqual(len(cols), 112)
			hits = [j for j, v in enumerate(cols) if v == 100.0]
			counts.add(len(hits))
			self.assertTrue(hits, "t_end=%d lost the spike: %s" % (t_end, cols))
			idx = max(hits)
			if prev_idx is not None:
				self.assertLessEqual(abs(idx - prev_idx), 1, "t_end=%d spike jumped from col %d to %d" % (t_end, prev_idx, idx))
				self.assertLessEqual(idx, prev_idx, "t_end=%d spike moved right, from col %d to %d" % (t_end, prev_idx, idx))
			prev_idx = idx
		self.assertEqual(counts, {1}, "the spike spanned a varying number of columns: %s" % sorted(counts))

		# The columns are fixed slices of time, so one more second either leaves them alone or scrolls them left by exactly one.
		d = state.Ring(24 * 3600, how="max")
		for s in range(base, base + 1000):
			d.put(s, float(s % 997))
		prev = d.cols(S + 20, 300, 112)
		for t_end in range(S + 21, S + 61):
			cur = d.cols(t_end, 300, 112)
			self.assertTrue(
				cur == prev or cur[:-1] == prev[1:],
				"t_end=%d re-sliced the window instead of scrolling: %s vs %s" % (t_end, cur[:4], prev[:4]),
			)
			prev = cur

	def test_ring_default_kind_works(self):
		r = state.Ring(3600)
		self.assertEqual(r.how, "max")
		now = 1700000000
		r.put(now, 5.0)
		cols = r.cols(now, 300, 10)
		self.assertEqual(len(cols), 10)
		self.assertEqual(cols[-1], 5.0)

	def test_ring_put_run_equals_n_puts_all_how(self):
		"""put_run equals n put calls for every how, crossing 10s and 60s boundaries and overlapping filled seconds."""
		rng = random.Random(12345)
		base = 1700000000

		for kind in ("max", "min", "mean", "last"):
			r_ref = state.Ring(3600, how=kind)
			r_run = state.Ring(3600, how=kind)

			# 1. Runs crossing 10s and 60s boundaries
			cur = base
			for _ in range(50):
				n = rng.randint(1, 75)
				val = rng.uniform(-100.0, 100.0)
				for dt in range(n):
					r_ref.put(cur + dt, val)
				r_run.put_run(cur, n, val)
				cur += n + rng.randint(0, 15)

			# 2. Runs overlapping already-filled seconds
			for _ in range(30):
				start = base + rng.randint(0, 1500)
				n = rng.randint(1, 40)
				val = rng.uniform(-100.0, 100.0)
				for dt in range(n):
					r_ref.put(start + dt, val)
				r_run.put_run(start, n, val)

			# Compare per-second values
			for s in range(base, cur):
				v_ref, v_run = r_ref.at(s), r_run.at(s)
				if math.isnan(v_ref) or math.isnan(v_run):
					self.assertTrue(math.isnan(v_ref) and math.isnan(v_run), "how=%s at(%d) nan mismatch" % (kind, s))
				else:
					self.assertAlmostEqual(v_ref, v_run, places=4, msg="how=%s at(%d) mismatch" % (kind, s))

			# Compare 10s and 60s summaries via cols()
			for secs in (300, 900, 3600):
				cols_ref = r_ref.cols(cur, secs, 60, kind)
				cols_run = r_run.cols(cur, secs, 60, kind)
				self.assertEqual(len(cols_ref), len(cols_run))
				for j in range(len(cols_ref)):
					c1, c2 = cols_ref[j], cols_run[j]
					if math.isnan(c1) or math.isnan(c2):
						self.assertTrue(math.isnan(c1) and math.isnan(c2), "how=%s cols(%d) col %d nan mismatch" % (kind, secs, j))
					else:
						self.assertAlmostEqual(c1, c2, places=4, msg="how=%s cols(%d) col %d mismatch" % (kind, secs, j))


CASES = [
	("router_mode_preset_loaded_and_flags_parsed", TestState("test_router_mode_preset_loaded_and_flags_parsed")),
	("single_model_mode_process_counted", TestState("test_single_model_mode_process_counted")),
	("router_flagless_loaded_model_no_crash_and_notice", TestState("test_router_flagless_loaded_model_no_crash_and_notice")),
	("single_model_process_not_found_notice_and_no_crash", TestState("test_single_model_process_not_found_notice_and_no_crash")),
	("slots_401_and_api_key_header", TestState("test_slots_401_and_api_key_header")),
	("slots_400_not_loaded_is_normal_between_polls", TestState("test_slots_400_not_loaded_is_normal_between_polls")),
	("uma_zero_vram_adapter", TestState("test_uma_zero_vram_adapter")),
	("amd_adapter_load_drives_gpu_series", TestState("test_amd_adapter_load_drives_gpu_series")),
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
	("rate_survives_sample_where_only_n_prompt_tokens_grows", TestState("test_rate_survives_sample_where_only_n_prompt_tokens_grows")),
	("long_prefill_recorded_sequence_phase_and_progress", TestState("test_long_prefill_recorded_sequence_phase_and_progress")),
	("long_prefill_stale_first_sample_never_draws_a_bar", TestState("test_long_prefill_stale_first_sample_never_draws_a_bar")),
	("damaged_history_lines_do_not_stop_the_start", TestState("test_damaged_history_lines_do_not_stop_the_start")),
	("prompt_total_narrows_so_the_bar_never_steps_back", TestState("test_prompt_total_narrows_so_the_bar_never_steps_back")),
	("long_prefill_bar_from_the_log_never_goes_back", TestState("test_long_prefill_bar_from_the_log_never_goes_back")),
	("busy_slot_stays_on_screen", TestState("test_busy_slot_stays_on_screen")),
	("decode_context_line_counts_generated_tokens_once", TestState("test_decode_context_line_counts_generated_tokens_once")),
	("collector_stale_sample_updated_seconds_ago", TestState("test_collector_stale_sample_updated_seconds_ago")),
	("slots_reply_bad_types_and_empty", TestState("test_slots_reply_bad_types_and_empty")),
	("request_log_trimmed_only_when_persisting", TestState("test_request_log_trimmed_only_when_persisting")),
	("requests_in_memory_capped", TestState("test_requests_in_memory_capped")),
	("calibration_write_is_atomic", TestState("test_calibration_write_is_atomic")),
	("calibration_load_drops_spilled_and_negative", TestState("test_calibration_load_drops_spilled_and_negative")),
	("forget_presets_removes_requests_and_calibration", TestState("test_forget_presets_removes_requests_and_calibration")),
	("calibration_sample_gates", TestState("test_calibration_sample_gates")),
	("fit_projection_cached_per_configuration", TestState("test_fit_projection_cached_per_configuration")),
	("fit_cache_persisted_and_reloaded", TestState("test_fit_cache_persisted_and_reloaded")),
	("fit_params_policy", TestState("test_fit_params_policy")),
	("ring_random_data_24h_bucket_aligned", TestRing("test_ring_random_data_24h_bucket_aligned")),
	("ring_short_windows_legacy_exact", TestRing("test_ring_short_windows_legacy_exact")),
	("ring_overwrite_keeps_buckets_exact", TestRing("test_ring_overwrite_keeps_buckets_exact")),
	("ring_stale_buckets_read_empty", TestRing("test_ring_stale_buckets_read_empty")),
	("ring_cols_are_fixed_slices", TestRing("test_ring_cols_are_fixed_slices")),
	("ring_default_kind_works", TestRing("test_ring_default_kind_works")),
	("models_backoff_connection_error_and_reset", TestState("test_models_backoff_connection_error_and_reset")),
	("ring_put_run_equals_n_puts_all_how", TestRing("test_ring_put_run_equals_n_puts_all_how")),
	("opener_for_caching_and_rebuild", TestState("test_opener_for_caching_and_rebuild")),
	("history_load_skips_old_day_file_unopened", TestState("test_history_load_skips_old_day_file_unopened")),
	("down_loopback_server_fails_fast", TestState("test_down_loopback_server_fails_fast")),
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
