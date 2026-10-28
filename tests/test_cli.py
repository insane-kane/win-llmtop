#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Aaron Kane <software@kanenet.us>
"""CLI and start-up tests for win-llmtop.

	python -I tools/win-llmtop/tests/test_cli.py
"""

import http.server
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import llmtop  # noqa: E402
from tests import _public_names  # noqa: E402

LLMTOP_PY = str(ROOT / "llmtop.py")


class _StubServer:
	"""Ephemeral HTTP server on 127.0.0.1 for testing CLI against llama-server."""

	def __init__(self, models_data, slots_data=None, require_key=None, slots_status=200):
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
		self.slots_status = slots_status
		self.requests = []
		parent = self

		class Handler(http.server.BaseHTTPRequestHandler):
			def do_GET(self):
				auth = self.headers.get("Authorization")
				parent.requests.append((self.path, auth))
				if self.path == "/models":
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


def _unused_port():
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
		s.bind(("127.0.0.1", 0))
		return s.getsockname()[1]


class TestCLI(unittest.TestCase):
	def test_help_generic_and_no_local_names(self):
		proc = subprocess.run([sys.executable, "-I", LLMTOP_PY, "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
		self.assertEqual(proc.returncode, 0)
		self.assertIn("usage: win-llmtop", proc.stdout)
		self.assertNotIn("D:" + "\\", proc.stdout)
		for name in _public_names.FORBIDDEN:
			self.assertNotIn(name.lower(), proc.stdout.lower())

	def test_fit_params_option_in_help(self):
		proc = subprocess.run([sys.executable, "-I", LLMTOP_PY, "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
		self.assertEqual(proc.returncode, 0)
		self.assertIn("--fit-params", proc.stdout)
		proc = subprocess.run([sys.executable, "-I", LLMTOP_PY, "--fit-params", "bogus"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
		self.assertEqual(proc.returncode, 2, "stderr: %s" % proc.stderr)
		self.assertIn("error:", proc.stderr)
		self.assertNotIn("Traceback", proc.stderr)

	def test_check_with_stub_server(self):
		models_data = [
			{
				"id": "test-preset",
				"object": "model",
				"status": {
					"value": "loaded",
					"args": ["llama-server.exe", "-m", "test.gguf", "-c", "8192"],
				},
			}
		]
		stub = _StubServer(models_data)
		secret_token = "secret-token-do-not-leak-998877"
		try:
			with tempfile.TemporaryDirectory() as tmpdir:
				cmd = [
					sys.executable, "-I", LLMTOP_PY,
					"--check",
					"--router", stub.url,
					"--state-dir", tmpdir,
					"--api-key", secret_token,
				]
				proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
				out = proc.stdout
				lines = [line.strip() for line in out.splitlines() if line.strip()]

				# Check server line is ok
				server_line = next((ln for ln in lines if ln.lower().startswith("server")), None)
				self.assertIsNotNone(server_line, "server line missing from --check output: %r" % out)
				self.assertIn("ok", server_line.split())

				# Check GPU line and exit code (ok or CPU-only both allow exit 0; FAIL forces exit 1)
				gpu_line = next((ln for ln in lines if ln.lower().startswith("gpu")), None)
				self.assertIsNotNone(gpu_line, "gpu line missing from --check output: %r" % out)
				gpu_parts = gpu_line.split()
				gpu_pass = ("ok" in gpu_parts) or ("--" in gpu_parts and "no GPU adapters (CPU-only)" in gpu_line)
				self.assertEqual(proc.returncode == 0, gpu_pass,
					"exit code %d does not match gpu pass (%s); out: %s" % (proc.returncode, gpu_pass, out))

				# Never leak API key
				self.assertNotIn(secret_token, proc.stdout)
				self.assertNotIn(secret_token, proc.stderr)
		finally:
			stub.close()

	def test_check_unused_port_server_unreachable(self):
		unused = _unused_port()
		with tempfile.TemporaryDirectory() as tmpdir:
			cmd = [
				sys.executable, "-I", LLMTOP_PY,
				"--check",
				"--router", "http://127.0.0.1:%d" % unused,
				"--state-dir", tmpdir,
			]
			proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
			self.assertEqual(proc.returncode, 1)
			combined = (proc.stdout + proc.stderr).lower()
			self.assertIn("server unreachable", combined)

	def test_once_page_all_runs_cleanly(self):
		models_data = [
			{
				"id": "test-preset",
				"object": "model",
				"status": {
					"value": "loaded",
					"args": ["llama-server.exe", "-m", "test.gguf", "-c", "8192"],
				},
			}
		]
		stub = _StubServer(models_data)
		try:
			with tempfile.TemporaryDirectory() as tmpdir:
				cmd = [
					sys.executable, "-I", LLMTOP_PY,
					"--once",
					"--page", "all",
					"--no-persist",
					"--router", stub.url,
					"--state-dir", tmpdir,
				]
				proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
				self.assertEqual(proc.returncode, 0, "stderr: %s" % proc.stderr)
				self.assertNotIn("traceback", proc.stdout.lower())
				self.assertNotIn("traceback", proc.stderr.lower())
		finally:
			stub.close()

	def test_once_flagless_model_pages_render_no_crash(self):
		# a router whose /models lists a loaded model with no status.args and no matching process
		models_data = [
			{
				"id": "flagless-preset",
				"object": "model",
				"status": {"value": "loaded"},
			}
		]
		stub = _StubServer(models_data)
		try:
			with tempfile.TemporaryDirectory() as tmpdir:
				for page in ("memory", "tensors", "live"):
					cmd = [
						sys.executable, "-I", LLMTOP_PY,
						"--once",
						"--page", page,
						"--no-persist",
						"--router", stub.url,
						"--state-dir", tmpdir,
					]
					proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
					self.assertEqual(proc.returncode, 0, "page %s stderr: %s" % (page, proc.stderr))
					self.assertNotIn("traceback", (proc.stdout + proc.stderr).lower())
					self.assertNotIn("render error", proc.stdout.lower())
					if page in ("memory", "tensors"):
						self.assertIn("could not analyse", proc.stdout)
		finally:
			stub.close()

	def test_check_flagless_model_no_crash(self):
		models_data = [
			{
				"id": "flagless-preset",
				"object": "model",
				"status": {"value": "loaded"},
			}
		]
		stub = _StubServer(models_data)
		try:
			with tempfile.TemporaryDirectory() as tmpdir:
				cmd = [
					sys.executable, "-I", LLMTOP_PY,
					"--check",
					"--router", stub.url,
					"--state-dir", tmpdir,
				]
				proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
				combined = proc.stdout + proc.stderr
				self.assertNotIn("traceback", combined.lower())
				self.assertNotIn("render error", combined.lower())
		finally:
			stub.close()

	def test_plan_flagless_model_no_crash(self):
		models_data = [
			{
				"id": "flagless-preset",
				"object": "model",
				"status": {"value": "loaded"},
			}
		]
		stub = _StubServer(models_data)
		try:
			with tempfile.TemporaryDirectory() as tmpdir:
				cmd = [
					sys.executable, "-I", LLMTOP_PY,
					"--plan",
					"--no-persist",
					"--router", stub.url,
					"--state-dir", tmpdir,
				]
				proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
				self.assertEqual(proc.returncode, 0, "stderr: %s" % proc.stderr)
				self.assertNotIn("traceback", (proc.stdout + proc.stderr).lower())
		finally:
			stub.close()

	def test_frame_text_render_error_frame_and_log(self):
		import os
		import llmtop
		from llmtop_lib import pages
		from types import SimpleNamespace
		with tempfile.TemporaryDirectory() as tmpdir:
			state_dir = os.path.join(tmpdir, "state")
			c = SimpleNamespace(
				alerts=SimpleNamespace(ordered=lambda: []),
				state_dir=state_dir,
				state_error=None,
			)
			snap = {"t": 1000.0, "router_up": True, "mode": "router"}
			view = pages.View()
			view.page = 1  # Memory
			llmtop._last_render_error_text = None
			def boom_a(*a, **k):
				raise RuntimeError("A119 forced render failure")
			def boom_b(*a, **k):
				raise ValueError("A119 a different forced failure")
			orig = pages.memory
			pages.memory = boom_a
			try:
				with mock.patch.object(os, "makedirs", wraps=os.makedirs) as makedirs_mock:
					lines = llmtop.frame_text(c, snap, view, 200, 40)
					text = "\n".join(lines)
					self.assertIn("llmtop", text)  # header
					self.assertIn("Memory", text)  # the page tab
					self.assertIn("q quit", text)  # footer keys
					self.assertIn("this page failed to render", text)  # body
					self.assertIn("RuntimeError", text)
					self.assertIn("press 1-6 to try another page; details:", text)
					log_path = os.path.join(state_dir, "llmtop-error.log")
					self.assertTrue(os.path.isfile(log_path))
					with open(log_path, encoding="utf-8") as f:
						log1 = f.read()
					self.assertIn("RuntimeError", log1)
					self.assertIn("page: Memory", log1)
					self.assertIn("version:", log1)
					self.assertIn("python:", log1)
					self.assertIn("platform:", log1)
					self.assertEqual(makedirs_mock.call_count, 1)  # written once
					# the same error is not re-written every tick
					llmtop.frame_text(c, snap, view, 200, 40)
					self.assertEqual(makedirs_mock.call_count, 1)  # not re-written
					# a different error re-writes the log
					pages.memory = boom_b
					llmtop.frame_text(c, snap, view, 200, 40)
					self.assertEqual(makedirs_mock.call_count, 2)  # re-written
					with open(log_path, encoding="utf-8") as f:
						log3 = f.read()
					self.assertIn("ValueError", log3)
					# page change still works after a render error
					llmtop.handle("1", c, view, snap, 40)
					self.assertEqual(view.page, 0)
			finally:
				pages.memory = orig

	def test_live_key_loop_keeps_working_after_render_error(self):
		import contextlib
		import io
		import llmtop
		from llmtop_lib import pages
		from types import SimpleNamespace
		snap = {"t": 1000.0, "router_up": True, "mode": "router"}
		c = SimpleNamespace(
			cfg={"hz": 1.0},
			tick=lambda t0=None: snap,
			alerts=SimpleNamespace(ordered=lambda: []),
			state_dir=None,
			state_error="no state folder",
		)
		view = pages.View()
		view.page = 1  # Memory
		llmtop._last_render_error_text = None
		def boom(*a, **k):
			raise RuntimeError("A119 forced render failure")
		orig = pages.memory
		pages.memory = boom
		buf = io.StringIO()
		try:
			with mock.patch.object(llmtop, "read_key", side_effect=["1", "q"]), contextlib.redirect_stdout(buf):
				llmtop.live(c, view)
		finally:
			pages.memory = orig
		self.assertEqual(view.page, 0)  # the "1" key changed the page after the render error

	def test_removed_pagein_warn_threshold_accepted_with_note(self):
		models_data = [
			{
				"id": "test-preset",
				"object": "model",
				"status": {
					"value": "loaded",
					"args": ["llama-server.exe", "-m", "test.gguf", "-c", "8192"],
				},
			}
		]
		stub = _StubServer(models_data)
		try:
			with tempfile.TemporaryDirectory() as tmpdir:
				cmd = [
					sys.executable, "-I", LLMTOP_PY,
					"--once",
					"--page", "all",
					"--no-persist",
					"--router", stub.url,
					"--state-dir", tmpdir,
					"--threshold", "pagein_warn=5",
				]
				proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
				self.assertEqual(proc.returncode, 0, "stderr: %s" % proc.stderr)
				self.assertNotIn("unknown or non-numeric threshold", proc.stderr)
				self.assertEqual(proc.stderr.count("win-llmtop: threshold pagein_warn was removed in 0.2.0 and is ignored"), 1, proc.stderr)

				# every other unknown name is still an error
				cmd = [sys.executable, "-I", LLMTOP_PY, "--once", "--page", "all", "--no-persist", "--router", stub.url, "--state-dir", tmpdir, "--threshold", "bogus=1"]
				proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
				self.assertEqual(proc.returncode, 1)
				self.assertIn("unknown or non-numeric threshold", proc.stderr)
		finally:
			stub.close()

	def test_read_key_case_insensitivity(self):
		# Test letters case-insensitivity
		test_cases = [
			("Q", "q"),
			("q", "q"),
			("P", "p"),
			("p", "p"),
			("S", "s"),
			("s", "s"),
			("G", "g"),
			("g", "g"),
			("M", "m"),
			("m", "m"),
			("F", "f"),
			("f", "f"),
			("T", "t"),
			("t", "t"),
			("D", "d"),
			("d", "d"),
		]
		with mock.patch("msvcrt.kbhit", return_value=True):
			for char, expected in test_cases:
				with mock.patch("msvcrt.getwch", return_value=char):
					self.assertEqual(llmtop.read_key(), expected)

			# Extended key (e.g. PgDn: '\xe0' followed by 'Q')
			with mock.patch("msvcrt.getwch", side_effect=["\xe0", "Q"]):
				self.assertEqual(llmtop.read_key(), "pgdn")

			# Extended key (Up: '\x00' followed by 'H')
			with mock.patch("msvcrt.getwch", side_effect=["\x00", "H"]):
				self.assertEqual(llmtop.read_key(), "up")

			# Ctrl+C and Escape
			with mock.patch("msvcrt.getwch", return_value="\x03"):
				self.assertEqual(llmtop.read_key(), "q")
			with mock.patch("msvcrt.getwch", return_value="\x1b"):
				self.assertEqual(llmtop.read_key(), "q")

			# Tab
			with mock.patch("msvcrt.getwch", return_value="\t"):
				self.assertEqual(llmtop.read_key(), "tab")

		with mock.patch("msvcrt.kbhit", return_value=False):
			self.assertIsNone(llmtop.read_key())

	def test_detect_style_from_env(self):
		self.assertEqual(llmtop.ui.detect_style({"WT_SESSION": "abc"}), "braille")
		self.assertEqual(llmtop.ui.detect_style({"TERM_PROGRAM": "vscode"}), "blocks")
		self.assertEqual(llmtop.ui.detect_style({"ConEmuANSI": "ON"}), "blocks")
		self.assertEqual(llmtop.ui.detect_style({}), "coarse")
		self.assertEqual(llmtop.ui.detect_style({"ConEmuANSI": "off"}), "coarse")
		# Windows Terminal wins over the other signals
		self.assertEqual(llmtop.ui.detect_style({"WT_SESSION": "abc", "TERM_PROGRAM": "vscode"}), "braille")

	def test_terminal_kind_from_env(self):
		self.assertEqual(llmtop.ui.terminal_kind({"WT_SESSION": "abc"}), "Windows Terminal")
		self.assertEqual(llmtop.ui.terminal_kind({"TERM_PROGRAM": "vscode"}), "other")
		self.assertEqual(llmtop.ui.terminal_kind({"ConEmuANSI": "ON"}), "other")
		self.assertEqual(llmtop.ui.terminal_kind({}), "classic console")

	def test_s_cycles_four_graph_styles(self):
		from types import SimpleNamespace
		cycle = ("braille", "blocks", "coarse", "ascii")
		old = llmtop.ui.STYLE["mode"]
		try:
			for cur, nxt in zip(cycle, cycle[1:] + cycle[:1], strict=True):
				llmtop.ui.STYLE["mode"] = cur
				view = SimpleNamespace(page=0, msg="")
				llmtop.handle("s", None, view, None, 40)
				self.assertEqual(llmtop.ui.STYLE["mode"], nxt)
				self.assertEqual(view.msg, "graph style: %s" % nxt)
		finally:
			llmtop.ui.STYLE["mode"] = old

	def _check_terminal_line(self, env_extra, extra_args=()):
		env = dict(os.environ)
		for k in ("WT_SESSION", "TERM_PROGRAM", "ConEmuANSI"):
			env.pop(k, None)
		env.update(env_extra)
		unused = _unused_port()
		with tempfile.TemporaryDirectory() as tmpdir:
			cmd = [sys.executable, "-I", LLMTOP_PY, "--check", "--router", "http://127.0.0.1:%d" % unused, "--state-dir", tmpdir]
			cmd += list(extra_args)
			proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=env)
		for ln in proc.stdout.splitlines():
			if ln.strip().startswith("terminal"):
				return ln
		self.fail("no terminal line in --check output: %r" % proc.stdout)

	def test_check_terminal_line(self):
		# classic console (no detection vars): coarse, with the note
		term = self._check_terminal_line({})
		self.assertIn("classic console: charts use the coarse style", term)
		self.assertIn("Windows Terminal draws finer charts", term)
		# Windows Terminal: braille, no note
		term = self._check_terminal_line({"WT_SESSION": "test"})
		self.assertIn("Windows Terminal: charts use the braille style", term)
		self.assertNotIn("Windows Terminal draws finer charts", term)
		# other modern terminal: blocks, no note
		term = self._check_terminal_line({"TERM_PROGRAM": "vscode"})
		self.assertIn("other: charts use the blocks style", term)
		self.assertNotIn("Windows Terminal draws finer charts", term)
		# an explicit --graph-style coarse does not get the note
		term = self._check_terminal_line({}, extra_args=("--graph-style", "coarse"))
		self.assertIn("charts use the coarse style", term)
		self.assertNotIn("Windows Terminal draws finer charts", term)

	def test_version_prints_version_copyright_licence(self):
		proc = subprocess.run([sys.executable, "-I", LLMTOP_PY, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
		self.assertEqual(proc.returncode, 0, "stderr: %s" % proc.stderr)
		lines = proc.stdout.splitlines()
		self.assertEqual(len(lines), 5, proc.stdout)
		self.assertTrue(lines[0].startswith("win-llmtop "), lines[0])
		self.assertIn("Copyright (C) 2026 Aaron Kane", lines[1])
		self.assertIn("License GPL-3.0-or-later", lines[2])
		self.assertIn("free software", lines[3])
		self.assertIn("NO WARRANTY", lines[4])

	def test_bad_option_values_exit_cleanly(self):
		for extra in (("--hz", "0"), ("--hz", "nan"), ("--history-hours", "0"), ("--width", "10"), ("--spill-mib", "-1")):
			proc = subprocess.run([sys.executable, "-I", LLMTOP_PY, *extra, "--once"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
			self.assertEqual(proc.returncode, 2, "args %r stderr: %s" % (extra, proc.stderr))
			self.assertIn("error:", proc.stderr)
			self.assertNotIn("Traceback", proc.stderr)

	def test_bad_threshold_values_exit_1(self):
		for value in ("avail_warn=abc", "avail_warn=inf", "hold_s=1.5"):
			proc = subprocess.run([sys.executable, "-I", LLMTOP_PY, "--once", "--no-persist", "--router", "http://127.0.0.1:1", "--threshold", value], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
			self.assertEqual(proc.returncode, 1, "threshold %r stderr: %s" % (value, proc.stderr))
			self.assertNotIn("Traceback", proc.stderr)
		# a whole number written as a float is accepted for an int threshold
		proc = subprocess.run([sys.executable, "-I", LLMTOP_PY, "--threshold", "hold_s=2.0", "--once", "--router", "http://127.0.0.1:1", "--no-persist"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
		self.assertEqual(proc.returncode, 0, "stderr: %s" % proc.stderr)
		self.assertNotIn("Traceback", proc.stderr)

	def test_plan_unknown_preset_exits_1(self):
		proc = subprocess.run([sys.executable, "-I", LLMTOP_PY, "--plan", "no-such-preset", "--router", "http://127.0.0.1:1", "--no-persist"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
		self.assertEqual(proc.returncode, 1, "stderr: %s" % proc.stderr)
		self.assertIn("unknown preset no-such-preset", proc.stderr)
		self.assertNotIn("Traceback", proc.stderr)

	def test_no_color_env_turns_colour_off(self):
		env = dict(os.environ)
		env["NO_COLOR"] = "1"
		proc = subprocess.run([sys.executable, "-I", LLMTOP_PY, "--once", "--router", "http://127.0.0.1:1", "--no-persist"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env=env)
		self.assertEqual(proc.returncode, 0, "stderr: %s" % proc.stderr)
		self.assertNotIn("\x1b[", proc.stdout)

	def test_forget_preset_option(self):
		with tempfile.TemporaryDirectory() as tmp:
			state_dir = os.path.join(tmp, "state")
			os.makedirs(state_dir)
			req_path = os.path.join(state_dir, "requests.jsonl")
			cal_path = os.path.join(state_dir, "calibration.json")
			with open(req_path, "w", encoding="utf-8") as f:
				f.write(json.dumps({"preset": "x", "pn": 100, "wall": 1700000000.0}) + "\n")
				f.write(json.dumps({"preset": "y", "pn": 200, "wall": 1700000100.0}) + "\n")
			with open(cal_path, "w", encoding="utf-8") as f:
				json.dump({"kx": {"v": 4, "overhead": 100, "at": 1700000100.0, "preset": "x"}}, f)
			# --no-persist does not stop the writes
			proc = subprocess.run([sys.executable, "-I", LLMTOP_PY, "--forget-preset", "x", "--forget-preset", "y", "--no-persist", "--state-dir", state_dir], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
			self.assertEqual(proc.returncode, 0, "stderr: %s" % proc.stderr)
			self.assertIn("forgot x: 1 request, 1 calibration entry", proc.stdout)
			self.assertIn("forgot y: 1 request, 0 calibration entries", proc.stdout)
			with open(req_path, encoding="utf-8") as f:
				self.assertEqual([json.loads(ln) for ln in f if ln.strip()], [])
			with open(cal_path, encoding="utf-8") as f:
				self.assertEqual(json.load(f), {})
			# a name with nothing stored: exit 1 and a line saying so
			proc = subprocess.run([sys.executable, "-I", LLMTOP_PY, "--forget-preset", "no-such-preset", "--state-dir", state_dir], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
			self.assertEqual(proc.returncode, 1, "stderr: %s" % proc.stderr)
			self.assertIn("nothing stored for no-such-preset", proc.stdout)

	def test_live_keys_read_when_tick_overruns_frame(self):
		"""Keys are read even when tick() takes longer than one frame (1/hz)."""
		import contextlib
		import io
		import llmtop
		from llmtop_lib import pages
		from types import SimpleNamespace

		snap = {"t": 1000.0, "router_up": False, "mode": None}
		tick_count = [0]
		fake_time = [1000.0]

		def slow_tick(t0=None):
			tick_count[0] += 1
			fake_time[0] += 1.0  # tick took 1.0s, frame period is 0.25s (hz=4)
			return snap

		c = SimpleNamespace(
			cfg={"hz": 4.0},
			tick=slow_tick,
			alerts=SimpleNamespace(ordered=lambda: []),
			state_dir=None,
			state_error="no state folder",
		)
		view = pages.View()
		buf = io.StringIO()

		with mock.patch("time.time", side_effect=lambda: fake_time[0]), \
		     mock.patch.object(llmtop, "read_key", return_value="q"), \
		     contextlib.redirect_stdout(buf):
			llmtop.live(c, view)

		self.assertLessEqual(tick_count[0], 2, "live() should have exited within two iterations")


CASES = [
	("help_generic_and_no_local_names", TestCLI("test_help_generic_and_no_local_names")),
	("fit_params_option_in_help", TestCLI("test_fit_params_option_in_help")),
	("check_with_stub_server", TestCLI("test_check_with_stub_server")),
	("check_unused_port_server_unreachable", TestCLI("test_check_unused_port_server_unreachable")),
	("once_page_all_runs_cleanly", TestCLI("test_once_page_all_runs_cleanly")),
	("once_flagless_model_pages_render_no_crash", TestCLI("test_once_flagless_model_pages_render_no_crash")),
	("check_flagless_model_no_crash", TestCLI("test_check_flagless_model_no_crash")),
	("plan_flagless_model_no_crash", TestCLI("test_plan_flagless_model_no_crash")),
	("frame_text_render_error_frame_and_log", TestCLI("test_frame_text_render_error_frame_and_log")),
	("live_key_loop_keeps_working_after_render_error", TestCLI("test_live_key_loop_keeps_working_after_render_error")),
	("removed_pagein_warn_threshold_accepted_with_note", TestCLI("test_removed_pagein_warn_threshold_accepted_with_note")),
	("read_key_case_insensitivity", TestCLI("test_read_key_case_insensitivity")),
	("detect_style_from_env", TestCLI("test_detect_style_from_env")),
	("terminal_kind_from_env", TestCLI("test_terminal_kind_from_env")),
	("s_cycles_four_graph_styles", TestCLI("test_s_cycles_four_graph_styles")),
	("check_terminal_line", TestCLI("test_check_terminal_line")),
	("version_prints_version_copyright_licence", TestCLI("test_version_prints_version_copyright_licence")),
	("bad_option_values_exit_cleanly", TestCLI("test_bad_option_values_exit_cleanly")),
	("bad_threshold_values_exit_1", TestCLI("test_bad_threshold_values_exit_1")),
	("plan_unknown_preset_exits_1", TestCLI("test_plan_unknown_preset_exits_1")),
	("no_color_env_turns_colour_off", TestCLI("test_no_color_env_turns_colour_off")),
	("forget_preset_option", TestCLI("test_forget_preset_option")),
	("live_keys_read_when_tick_overruns_frame", TestCLI("test_live_keys_read_when_tick_overruns_frame")),
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
