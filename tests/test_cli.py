#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI and start-up tests for win-llmtop.

	python -I tools/win-llmtop/tests/test_cli.py
"""

import http.server
import json
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


CASES = [
	("help_generic_and_no_local_names", TestCLI("test_help_generic_and_no_local_names")),
	("check_with_stub_server", TestCLI("test_check_with_stub_server")),
	("check_unused_port_server_unreachable", TestCLI("test_check_unused_port_server_unreachable")),
	("once_page_all_runs_cleanly", TestCLI("test_once_page_all_runs_cleanly")),
	("read_key_case_insensitivity", TestCLI("test_read_key_case_insensitivity")),
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
