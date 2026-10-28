#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Aaron Kane <software@kanenet.us>
"""Bounded I/O and non-blocking helper execution tests for llmtop.

	python -I tools/win-llmtop/tests/test_bounded_io.py
"""

import http.server
import json
import pathlib
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llmtop_lib import model, state  # noqa: E402
from tests.test_state import _patch_win, _StubServer  # noqa: E402


class TestBoundedIO(unittest.TestCase):
	def test_slow_sending_server(self):
		class SlowHandler(http.server.BaseHTTPRequestHandler):
			def do_GET(self):
				self.send_response(200)
				self.send_header("Content-Type", "application/json")
				self.end_headers()
				for _ in range(50):
					try:
						self.wfile.write(b" ")
						self.wfile.flush()
						time.sleep(0.2)
					except Exception:
						break

			def log_message(self, *a):
				pass

		srv = http.server.HTTPServer(("127.0.0.1", 0), SlowHandler)
		threading.Thread(target=srv.serve_forever, daemon=True).start()
		url = f"http://127.0.0.1:{srv.server_port}"
		try:
			with tempfile.TemporaryDirectory() as tmp:
				c = state.Collector(dict(state.CFG, router=url, state_dir=tmp, persist=False, calibrate=False))
				t0 = time.monotonic()
				with self.assertRaises((TimeoutError, OSError)):
					c.get("/models")
				elapsed_models = time.monotonic() - t0
				self.assertLess(elapsed_models, 1.3, f"elapsed {elapsed_models:.3f} >= 1.3s")
				self.assertGreaterEqual(elapsed_models, 0.8, f"elapsed {elapsed_models:.3f} < 0.8s")

				t0 = time.monotonic()
				with self.assertRaises((TimeoutError, OSError)):
					c.get("/slots")
				elapsed_slots = time.monotonic() - t0
				self.assertLess(elapsed_slots, 0.8, f"elapsed {elapsed_slots:.3f} >= 0.8s")
				self.assertGreaterEqual(elapsed_slots, 0.4, f"elapsed {elapsed_slots:.3f} < 0.4s")
		finally:
			srv.shutdown()
			srv.server_close()

	def test_never_answering_server(self):
		s = socket.socket()
		s.bind(("127.0.0.1", 0))
		s.listen(5)
		port = s.getsockname()[1]
		url = f"http://127.0.0.1:{port}"

		def accept_and_sleep():
			while True:
				try:
					conn, _ = s.accept()
					time.sleep(5)
					conn.close()
				except Exception:
					break

		threading.Thread(target=accept_and_sleep, daemon=True).start()
		try:
			with tempfile.TemporaryDirectory() as tmp:
				c = state.Collector(dict(state.CFG, router=url, state_dir=tmp, persist=False, calibrate=False))
				t0 = time.monotonic()
				with self.assertRaises((TimeoutError, OSError)):
					c.get("/models")
				elapsed_models = time.monotonic() - t0
				self.assertLess(elapsed_models, 1.3, f"elapsed {elapsed_models:.3f} >= 1.3s")

				t0 = time.monotonic()
				with self.assertRaises((TimeoutError, OSError)):
					c.get("/slots")
				elapsed_slots = time.monotonic() - t0
				self.assertLess(elapsed_slots, 0.8, f"elapsed {elapsed_slots:.3f} >= 0.8s")
		finally:
			s.close()

	def test_reply_size_cap_9mib(self):
		class BigHandler(http.server.BaseHTTPRequestHandler):
			def do_GET(self):
				payload = json.dumps({"data": "x" * (9 * 1024 * 1024)}).encode("utf-8")
				self.send_response(200)
				self.send_header("Content-Type", "application/json")
				self.send_header("Content-Length", str(len(payload)))
				self.end_headers()
				self.wfile.write(payload)

			def log_message(self, *a):
				pass

		srv = http.server.HTTPServer(("127.0.0.1", 0), BigHandler)
		threading.Thread(target=srv.serve_forever, daemon=True).start()
		url = f"http://127.0.0.1:{srv.server_port}"
		try:
			with tempfile.TemporaryDirectory() as tmp:
				c = state.Collector(dict(state.CFG, router=url, state_dir=tmp, persist=False, calibrate=False))
				with self.assertRaises(OSError):
					c.get("/models")
		finally:
			srv.shutdown()
			srv.server_close()

	def test_five_candidate_processes_round_robin(self):
		def_sock = socket.socket()
		def_sock.bind(("127.0.0.1", 0))
		def_sock.listen(5)
		def_port = def_sock.getsockname()[1]

		def close_immediately():
			while True:
				try:
					conn, _ = def_sock.accept()
					conn.close()
				except Exception:
					break

		threading.Thread(target=close_immediately, daemon=True).start()

		sockets = []
		ports = []
		for _ in range(5):
			sock = socket.socket()
			sock.bind(("127.0.0.1", 0))
			sock.listen(5)
			sockets.append(sock)
			ports.append(sock.getsockname()[1])

		def accept_and_sleep(s):
			while True:
				try:
					conn, _ = s.accept()
					time.sleep(5)
					conn.close()
				except Exception:
					break

		for s in sockets:
			threading.Thread(target=accept_and_sleep, args=(s,), daemon=True).start()

		procs = {1000 + i: ("llama-server.exe", 400) for i in range(5)}
		proc_info_map = {
			1000 + i: {
				"exe": "C:\\bin\\llama-server.exe",
				"argv": ["C:\\bin\\llama-server.exe", "--port", str(ports[i])],
				"created": 1700000000.0 + i,
			}
			for i in range(5)
		}

		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win(procs=procs, proc_info_map=proc_info_map, proc_mem_map={}):
				cfg = dict(state.CFG, router=None, state_dir=tmp, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.cfg["router"] = f"http://127.0.0.1:{def_port}"

				for i in range(5):
					t0 = time.monotonic()
					c.poll_models(1700000000.0 + i)
					elapsed = time.monotonic() - t0
					self.assertLessEqual(elapsed, 1.5, f"call {i} took {elapsed:.3f}s > 1.5s")
					expected_url = f"http://127.0.0.1:{ports[i]}"
					self.assertIn(expected_url, c.router_tried)
					self.assertEqual(len(c.router_tried), 2 + i)
		finally:
			def_sock.close()
			for s in sockets:
				s.close()

	def test_aliases_background_thread(self):
		srv = _StubServer(models_data=[
			{"id": "m1", "object": "model", "status": {"args": ["llama-server.exe", "-customflag", "42"]}}
		])
		exe_path = "C:\\bin\\llama-server.exe"

		def slow_aliases(exe):
			time.sleep(5)
			return {"customflag": "my-custom-flag"}

		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win(
				procs={2000: ("llama-server.exe", 400)},
				proc_info_map={2000: {"exe": exe_path, "argv": [exe_path], "created": 1700000000.0}},
				proc_mem_map={},
				listener_pid=2000,
			):
				cfg = dict(state.CFG, router=srv.url, state_dir=tmp, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda p: None

				with mock.patch.object(model, "server_aliases", side_effect=slow_aliases):
					t0 = time.monotonic()
					c.poll_models(1700000000.0)
					elapsed = time.monotonic() - t0
					self.assertLess(elapsed, 0.5, f"poll_models took {elapsed:.3f}s >= 0.5s")
					flags, _ = c.flags.get("m1", ({}, None))
					self.assertNotIn("my-custom-flag", flags)

					t = c._alias_threads.get(exe_path)
					if t:
						t.join(timeout=7)

					c.poll_models(1700000001.0)
					flags, _ = c.flags.get("m1", ({}, None))
					self.assertEqual(flags.get("my-custom-flag"), "42")
		finally:
			srv.close()

	def test_full_tick_never_answering(self):
		s = socket.socket()
		s.bind(("127.0.0.1", 0))
		s.listen(5)
		port = s.getsockname()[1]
		url = f"http://127.0.0.1:{port}"

		def accept_and_sleep():
			while True:
				try:
					conn, _ = s.accept()
					time.sleep(5)
					conn.close()
				except Exception:
					break

		threading.Thread(target=accept_and_sleep, daemon=True).start()
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win(procs={}, proc_info_map={}, proc_mem_map={}):
				cfg = dict(state.CFG, router=url, state_dir=tmp, persist=False, calibrate=False)
				c = state.Collector(cfg)
				c.ensure_map = lambda p: None
				t0 = time.monotonic()
				c.tick(1700000000.0)
				elapsed = time.monotonic() - t0
				self.assertLessEqual(elapsed, 1.6, f"tick took {elapsed:.3f}s > 1.6s")
		finally:
			s.close()


CASES = [
	("slow_sending_server", TestBoundedIO("test_slow_sending_server")),
	("never_answering_server", TestBoundedIO("test_never_answering_server")),
	("reply_size_cap_9mib", TestBoundedIO("test_reply_size_cap_9mib")),
	("five_candidate_processes_round_robin", TestBoundedIO("test_five_candidate_processes_round_robin")),
	("aliases_background_thread", TestBoundedIO("test_aliases_background_thread")),
	("full_tick_never_answering", TestBoundedIO("test_full_tick_never_answering")),
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
