#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Aaron Kane <software@kanenet.us>
"""Trust-boundary tests for win-llmtop: what a server's say-so may start, open or receive.

	A program is started only from the folder of the running llama-server that serves the watched URL; a file path a server reports is opened
	only when that server is trusted and the path is absolute on a local drive; the API key goes only to a URL the user gave or one taken
	from a running llama-server's command line, and no redirect is followed. Nothing here depends on what is installed: every program is a
	batch file in a temporary folder, and every server is a stub on the loopback address.

	python -I tools/win-llmtop/tests/test_trust.py
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
import time
import unittest
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llmtop_lib import model, state, win  # noqa: E402
from tests.test_state import _patch_win  # noqa: E402

LLMTOP_PY = str(ROOT / "llmtop.py")
REAL_RUN = subprocess.run
NOW = 1700000100.0
SERVER_EXE = "C:\\bin\\llama-server.exe"


class _Stub:
	"""A llama-server stand-in on the loopback address that records (path, Authorization header) per request."""

	def __init__(self, models_data, require_key=None, redirect_to=None, redirect_code=302):
		self.models_data, self.require_key, self.redirect_to, self.redirect_code = models_data, require_key, redirect_to, redirect_code
		self.requests = []
		parent = self

		class Handler(http.server.BaseHTTPRequestHandler):
			def do_GET(self):
				auth = self.headers.get("Authorization")
				parent.requests.append((self.path, auth))
				if parent.redirect_to:
					self.send_response(parent.redirect_code)
					self.send_header("Location", parent.redirect_to + ("/models" if self.path == "/models" else "/"))
					self.send_header("Content-Length", "0")
					self.end_headers()
				elif parent.require_key and auth != "Bearer " + parent.require_key:
					self._json(401, {"error": {"code": 401}})
				elif self.path == "/models":
					self._json(200, {"object": "list", "data": parent.models_data})
				else:
					self._json(404, {"error": "not found"})

			def _json(self, code, payload):
				raw = json.dumps(payload).encode("utf-8")
				self.send_response(code)
				self.send_header("Content-Type", "application/json")
				self.send_header("Content-Length", str(len(raw)))
				self.end_headers()
				self.wfile.write(raw)

			def log_message(self, fmt, *args):
				pass

		self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
		self.port = self.httpd.server_port
		self.url = "http://127.0.0.1:%d" % self.port
		threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

	def close(self):
		self.httpd.shutdown()
		self.httpd.server_close()


class _Proxy:
	"""A stand-in HTTP proxy on the loopback address: records (request target, Authorization header) and answers every GET with one model."""

	def __init__(self):
		self.requests = []
		parent = self

		class Handler(http.server.BaseHTTPRequestHandler):
			def do_GET(self):
				parent.requests.append((self.path, self.headers.get("Authorization")))
				raw = json.dumps({"object": "list", "data": [{"id": "m", "object": "model"}]}).encode("utf-8")
				self.send_response(200)
				self.send_header("Content-Type", "application/json")
				self.send_header("Content-Length", str(len(raw)))
				self.end_headers()
				self.wfile.write(raw)

			def log_message(self, fmt, *args):
				pass

		self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
		self.url = "http://127.0.0.1:%d" % self.httpd.server_port
		threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

	def env(self):
		"""Environment variables that point every scheme at this proxy, in both spellings."""
		return {k: self.url for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")}

	def close(self):
		self.httpd.shutdown()
		self.httpd.server_close()


def _batch(path, marker, help_text=""):
	"""A harmless program: a batch file that leaves `marker` beside itself when run, and prints `help_text`."""
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w", encoding="ascii", newline="\r\n") as f:
		f.write("@echo off\necho ran> \"%%~dp0%s\"\n%s\n" % (marker, help_text))
	return path


def _status_models(args):
	return [{"id": "p", "object": "model", "status": {"value": "loaded", "args": args}}]


def _cfg(tmp, url, **over):
	return dict(state.CFG, router=url, state_dir=os.path.join(tmp, "state"), log_dir=None, persist=False, calibrate=False, **over)


def _recording_run(calls):
	"""A subprocess.run that records the argument vector of every program and then really runs it."""
	def run(argv, *a, **k):
		calls.append(list(argv))
		return REAL_RUN(argv, *a, **k)
	return run


def _trusted_win(port_holder_pid=7001, exe=SERVER_EXE):
	"""_patch_win with one llama-server process (`port_holder_pid`, image `exe`) that owns whatever port the watched URL names."""
	procs = {port_holder_pid: (os.path.basename(exe).lower(), 400)}
	info = {port_holder_pid: {"exe": exe, "argv": [exe, "--port", "1"], "created": 1700000000.0}}
	return _patch_win(procs=procs, proc_info_map=info, proc_mem_map={}, listener_pid=port_holder_pid)


class TestTrust(unittest.TestCase):
	def test_args0_of_an_untrusted_server_starts_nothing(self):
		"""Old code ran `<status.args[0]> --help` in poll_models and llama-fit-params from that folder in map_job."""
		with tempfile.TemporaryDirectory() as tmp:
			evil = _batch(os.path.join(tmp, "evil", "llama-server.bat"), "evil_ran.txt")
			open(os.path.join(tmp, "evil", "llama-fit-params.exe"), "wb").close()
			stub = _Stub(_status_models([evil, "--model", "C:\\x\\m.gguf", "-c", "4096"]))
			calls = []
			try:
				with _patch_win(procs={}, guard=False), mock.patch("subprocess.run", side_effect=_recording_run(calls)):
					c = state.Collector(_cfg(tmp, stub.url))
					c.poll_models(NOW)
					self.assertIn("p", c.flags)  # the flags are still read from status.args
					self.assertEqual(c.flags["p"][0].get("ctx-size"), "4096")
					self.assertIsNone(c.watched_exe())
					c.ensure_map("p")
					self.assertEqual(c.mem["p"]["state"], "error")
					self.assertIn("not used because", c.mem["p"]["error"])
					flags, exe = c.flags["p"]
					c.map_job("p", flags, "key")  # even called directly, with the server's own program as the hint
					self.assertEqual(c.mem["p"]["state"], "error")
			finally:
				stub.close()
			self.assertEqual(calls, [])
			self.assertFalse(os.path.exists(os.path.join(tmp, "evil", "evil_ran.txt")))

	def test_trusted_server_runs_only_its_own_help_and_never_args0(self):
		"""Old code ran args[0] even when the server was the machine's own llama-server."""
		with tempfile.TemporaryDirectory() as tmp:
			mine = _batch(os.path.join(tmp, "mine", "llama-server.bat"), "mine_ran.txt", "echo -c, --ctx-size N                  prompt context (default: 4096)")
			evil = _batch(os.path.join(tmp, "evil", "llama-server.bat"), "evil_ran.txt")
			open(os.path.join(tmp, "evil", "llama-fit-params.exe"), "wb").close()
			stub = _Stub(_status_models([evil, "-c", "2048", "--model", "C:\\x\\m.gguf"]))
			calls = []
			try:
				with _trusted_win(exe=mine), mock.patch("subprocess.run", side_effect=_recording_run(calls)):
					c = state.Collector(_cfg(tmp, stub.url))
					c.poll_models(NOW)
					self.assertEqual(c.watched_exe(), mine)
					self.assertEqual(c.flags["p"][0].get("ctx-size"), "2048")  # short flags are read with the trusted server's own alias table
					self.assertEqual(c.flags["p"][1], mine)
					c.map_job("p", c.flags["p"][0], "key")  # no llama-fit-params next to the trusted server; the GGUF is not there either
					self.assertEqual(c.mem["p"]["state"], "error")
			finally:
				stub.close()
			self.assertEqual(calls, [[mine, "--help"]])
			self.assertTrue(os.path.exists(os.path.join(tmp, "mine", "mine_ran.txt")))
			self.assertFalse(os.path.exists(os.path.join(tmp, "evil", "evil_ran.txt")))

	def test_fit_projection_looks_only_in_the_servers_folder(self):
		"""Old code also tried the current folder, PATH and the folder named by status.args[0]."""
		with tempfile.TemporaryDirectory() as tmp:
			mine = os.path.join(tmp, "mine", "llama-server.exe")
			os.makedirs(os.path.dirname(mine))
			open(mine, "wb").close()
			for d in ("cwd", "onpath"):
				os.makedirs(os.path.join(tmp, d))
				open(os.path.join(tmp, d, "llama-fit-params.exe"), "wb").close()
			flags = {"model": "C:\\models\\m.gguf", "ctx-size": "4096"}
			here = os.getcwd()
			done = mock.Mock(stdout="", stderr="")
			try:
				os.chdir(os.path.join(tmp, "cwd"))
				with mock.patch.dict(os.environ, {"PATH": os.path.join(tmp, "onpath") + os.pathsep + os.environ.get("PATH", "")}), \
						mock.patch("subprocess.run", return_value=done) as run:
					self.assertIsNone(model.fit_projection(mine, flags))
					self.assertIsNone(model.fit_projection(None, flags))
					self.assertIsNone(model.fit_projection("llama-server.exe", flags))
					run.assert_not_called()
					beside = os.path.join(tmp, "mine", "llama-fit-params.exe")
					open(beside, "wb").close()
					model.fit_projection(mine, flags)
					self.assertEqual(run.call_args[0][0][0], beside)
					run.reset_mock()
					self.assertIsNone(model.fit_projection(mine, {"model": "\\\\host\\share\\m.gguf"}))  # a network model path never reaches the helper
					run.assert_not_called()
			finally:
				os.chdir(here)

	def test_server_defaults_runs_only_an_absolute_existing_server_path(self):
		"""Old code resolved a bare or relative name through the current folder and PATH, then ran it."""
		with tempfile.TemporaryDirectory() as tmp:
			mine = _batch(os.path.join(tmp, "mine", "llama-server.bat"), "mine_ran.txt", "echo -c, --ctx-size N                  prompt context (default: 4096)")
			decoy = _batch(os.path.join(tmp, "cwd", "llama-server.bat"), "decoy_ran.txt")
			here = os.getcwd()
			try:
				os.chdir(os.path.join(tmp, "cwd"))
				with mock.patch.dict(os.environ, {"PATH": os.path.join(tmp, "cwd") + os.pathsep + os.environ.get("PATH", "")}):
					for bad in (None, "", "llama-server.bat", ".\\llama-server.bat", os.path.join(tmp, "absent", "llama-server.bat"), os.path.join(tmp, "mine"), 7):
						self.assertEqual(dict(model.server_defaults(bad)), {}, bad)
						self.assertEqual(model.server_aliases(bad), {}, bad)
					self.assertFalse(os.path.exists(decoy.replace("llama-server.bat", "decoy_ran.txt")))
					self.assertFalse(os.path.exists(os.path.join(tmp, "mine", "mine_ran.txt")))
					self.assertEqual(model.server_defaults(mine).get("ctx-size"), "4096")
					self.assertEqual(model.server_aliases(mine).get("c"), "ctx-size")
					self.assertTrue(os.path.exists(os.path.join(tmp, "mine", "mine_ran.txt")))
			finally:
				os.chdir(here)

	def test_redirect_is_an_error_and_never_carries_the_key(self):
		"""Old code followed a redirect with the Authorization header attached and returned the second server's data."""
		for code in (301, 302, 303, 307, 308):
			second = _Stub([{"id": "stolen", "object": "model"}])
			first = _Stub([], redirect_to=second.url, redirect_code=code)
			try:
				with _patch_win(procs={}):
					with tempfile.TemporaryDirectory() as tmp:
						c = state.Collector(_cfg(tmp, first.url, api_key="redirect-key-1"))
						c.poll_models(NOW)
						self.assertFalse(c.router_up, code)
						self.assertEqual(c.models, {}, code)
						self.assertIn(str(code), c.router_error)
			finally:
				first.close()
				second.close()
			self.assertEqual(first.requests, [("/models", "Bearer redirect-key-1")], code)  # the URL the user gave gets the key once
			self.assertEqual(second.requests, [], code)  # the redirect target is never contacted, so it can never see a header

	def test_default_probe_sends_no_key_and_an_explicit_router_does(self):
		"""Old code attached the key to every request, the default-address probe included."""
		models = [{"id": "m", "object": "model"}]
		stub = _Stub(models, require_key="probe-key-2")
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win(procs={}):
				c = state.Collector(dict(_cfg(tmp, None), api_key="probe-key-2"))
				self.assertFalse(c.router_explicit)
				c.cfg["router"] = stub.url  # stands in for the default address
				snap = c.tick(NOW)
				self.assertEqual([auth for _, auth in stub.requests], [None])
				self.assertEqual(snap["router_auth_error"], "HTTP 401")
				self.assertIn("--router", c.router_error)
				self.assertIn("--router", c.alerts.active["router"]["msg"])
				self.assertLessEqual(len(c.alerts.active["router"]["msg"]), 72)
				self.assertFalse(snap["router_key_allowed"])
			stub.requests.clear()
			with tempfile.TemporaryDirectory() as tmp, _patch_win(procs={}):
				c = state.Collector(dict(_cfg(tmp, stub.url), api_key="probe-key-2"))
				c.poll_models(NOW)
				self.assertTrue(c.router_up)
				self.assertEqual(stub.requests, [("/models", "Bearer probe-key-2")])
		finally:
			stub.close()

	def test_key_goes_to_a_url_matched_to_a_running_llama_server(self):
		"""A normal user: the default address needs the key, and a running llama-server's own command line names it, so it is sent."""
		stub = _Stub([{"id": "m", "object": "model"}], require_key="match-key-3")
		procs = {5000: ("llama-server.exe", 400)}
		info = {5000: {"exe": SERVER_EXE, "argv": [SERVER_EXE, "--port", str(stub.port)], "created": 1700000000.0}}
		try:
			with tempfile.TemporaryDirectory() as tmp, _patch_win(procs=procs, proc_info_map=info, proc_mem_map={}):
				c = state.Collector(dict(_cfg(tmp, None), api_key="match-key-3"))
				c.cfg["router"] = stub.url
				c.poll_models(NOW)
				self.assertTrue(c.router_up)
				self.assertEqual([auth for _, auth in stub.requests], [None, "Bearer match-key-3"])
				self.assertIn("(pid 5000)", c.router_discovery)
		finally:
			stub.close()

	def test_local_path_rule(self):
		"""Old code opened every path a server reported: UNC, device and relative paths included."""
		good = ["C:\\models\\m.gguf", "d:/models/m.gguf", "C:\\x", "Z:\\a b\\c.gguf"]
		bad = ["\\\\host\\share\\x.gguf", "//host/x", "\\\\?\\C:\\x", "\\\\.\\pipe\\x", "\\\\?\\UNC\\host\\share\\x", "//?/C:/x", "//./pipe/x", "relative\\x.gguf", "x.gguf",
			"C:x.gguf", "\\x.gguf", "/x.gguf", "", None, True, 5, "C:\\CON", "C:\\a\\nul.gguf", "C:\\x.gguf:stream", "C:\\x\x00.gguf", "C:\\*.gguf", "http://host/x.gguf"]
		for p in good:
			self.assertTrue(model.local_path(p), p)
		for p in bad:
			self.assertFalse(model.local_path(p), repr(p))
		for p in bad:  # the model reader refuses the same paths on its own
			if p is not None and not isinstance(p, int):
				self.assertRaises(ValueError, model.analyze, {"model": p})

	def test_loopback_servers_bypass_the_proxy_and_remote_ones_use_it(self):
		"""Old code sent every request through whatever proxy the environment named, loopback ones included."""
		proxy = _Proxy()
		stub = _Stub([{"id": "m", "object": "model"}])
		before = dict(os.environ)
		try:
			with mock.patch.dict(os.environ, proxy.env()):
				for name in ("NO_PROXY", "no_proxy"):
					os.environ.pop(name, None)
				with tempfile.TemporaryDirectory() as tmp, _patch_win(procs={}):
					c = state.Collector(_cfg(tmp, stub.url, api_key="proxy-key-4"))
					c.poll_models(NOW)
					self.assertTrue(c.router_up)
					self.assertEqual(stub.requests, [("/models", "Bearer proxy-key-4")])  # straight to the loopback server
					self.assertEqual(proxy.requests, [])  # the proxy saw nothing
					for url in ("http://[::1]:1", "http://localhost:1"):  # nothing listens there; the proxy must still not be asked
						c = state.Collector(_cfg(tmp, url))
						c.poll_models(NOW)
						self.assertFalse(c.router_up, url)
					self.assertEqual(proxy.requests, [])
					remote = "http://llmtop-test.invalid:8080"  # a name that cannot resolve: only a proxy can answer
					c = state.Collector(_cfg(tmp, remote, api_key="proxy-key-4"))  # a URL the user gave: the key goes with it
					c.poll_models(NOW)
					self.assertTrue(c.router_up)
					self.assertEqual(proxy.requests, [(remote + "/models", "Bearer proxy-key-4")])  # the absolute URL, as a proxy is asked
					proxy.requests.clear()
					c = state.Collector(dict(_cfg(tmp, None), api_key="proxy-key-4"))
					c.cfg["router"] = remote  # stands in for the default address: key_allowed is False, so no key
					c.poll_models(NOW)
					self.assertTrue(c.router_up)
					self.assertEqual(proxy.requests, [(remote + "/models", None)])
				self.assertEqual(stub.requests, [("/models", "Bearer proxy-key-4")])
			self.assertEqual(dict(os.environ), before)  # patch.dict put the environment back, whatever the test removed
		finally:
			stub.close()
			proxy.close()

	def test_opener_is_built_per_request_from_the_current_environment(self):
		"""The system proxy settings are read when the request is made, not when the module is imported."""
		with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://proxy-one.invalid:3128", "http_proxy": "http://proxy-one.invalid:3128"}):
			first = state.opener_for("http://remote.invalid:8080")
			os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = "http://proxy-two.invalid:3128"
			second = state.opener_for("http://remote.invalid:8080")
			loop = state.opener_for("http://127.0.0.1:8080")

		def proxies(opener):
			return next(h.proxies for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler))

		self.assertEqual(proxies(first)["http"], "http://proxy-one.invalid:3128")
		self.assertEqual(proxies(second)["http"], "http://proxy-two.invalid:3128")
		self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) for h in loop.handlers))  # an empty proxy table adds no handler: nothing is proxied
		for o in (first, second, loop):  # every opener refuses redirects
			self.assertTrue(any(isinstance(h, state._NoRedirect) for h in o.handlers))
			self.assertFalse(any(type(h) is urllib.request.HTTPRedirectHandler for h in o.handlers))

	def _collector(self, tmp, stub, **over):
		return state.Collector(_cfg(tmp, stub.url, **over))

	def test_untrusted_paths_are_skipped_with_a_notice_never_an_exception(self):
		"""Old code used the model, draft and log-file paths as given."""
		stub = _Stub([{"id": "p", "object": "model", "status": {"value": "loaded"}}])
		try:
			with tempfile.TemporaryDirectory() as tmp, _trusted_win():
				c = self._collector(tmp, stub)
				c.poll_models(NOW)
				self.assertEqual(c.watched_exe(), SERVER_EXE)
				c.flags["p"] = ({"log-file": "\\\\host\\share\\p.log", "model": "\\\\host\\share\\m.gguf", "model-draft": "d.gguf"}, SERVER_EXE)
				self.assertIsNone(c.preset_log_path("p"))
				self.assertIn("p: the log file path is ignored: it is not an absolute path on a local drive", c.rejected.values())
				c.ensure_map("p")  # must not raise
				self.assertEqual(c.mem["p"]["state"], "error")
				self.assertIn("local drive", c.mem["p"]["error"])
				c.ensure_map("p")  # not rewritten every frame
				self.assertIn("p: the model path is ignored: it is not an absolute path on a local drive", [n for n in c._build_notices({"p": {"status": "loaded"}}, [1])])
				c.flags["p"] = ({"log-file": "C:\\logs\\p.log"}, SERVER_EXE)
				self.assertEqual(c.preset_log_path("p"), "C:\\logs\\p.log")
				self.assertNotIn(("p", "log-file"), c.rejected)
		finally:
			stub.close()

	def test_analysis_gets_only_vetted_flags_and_the_trusted_program(self):
		"""Old code handed the server's draft path to the GGUF reader and the server's program folder to the projection."""
		stub = _Stub([{"id": "p", "object": "model", "status": {"value": "loaded"}}])
		seen = {}

		def fake_fit(exe, flags, timeout=60):
			seen["exe"] = exe
			return None

		def fake_analyze(flags, fit=None, defaults=None, n_slots=None):
			seen["flags"] = dict(flags)
			return {"stub": True}

		try:
			with tempfile.TemporaryDirectory() as tmp, _trusted_win(), mock.patch.object(model, "fit_projection", fake_fit), mock.patch.object(model, "analyze", fake_analyze):
				c = self._collector(tmp, stub)
				c.poll_models(NOW)
				c.flags["p"] = ({"model": "C:\\models\\m.gguf", "model-draft": "\\\\host\\share\\d.gguf", "spec-draft-model": "//host/d.gguf", "ctx-size": "4096"}, SERVER_EXE)
				c.ensure_map("p")
				deadline = time.time() + 5
				while c.mem["p"]["state"] == "pending" and time.time() < deadline:
					time.sleep(0.02)
				self.assertEqual(c.mem["p"]["state"], "ready", c.mem["p"])
				self.assertEqual(seen["exe"], SERVER_EXE)
				self.assertEqual(seen["flags"]["model"], "C:\\models\\m.gguf")
				self.assertNotIn("model-draft", seen["flags"])
				self.assertNotIn("spec-draft-model", seen["flags"])
				self.assertIn("p: the draft model path is ignored: it is not an absolute path on a local drive", c.rejected.values())
				self.assertIn("model-draft", c.flags["p"][0])  # what the server said is kept as it was
		finally:
			stub.close()

	def test_a_server_that_is_not_on_loopback_or_not_llama_server_is_not_trusted(self):
		"""Old code trusted every server's paths."""
		cases = [
			("a non-loopback address", dict(listener_pid=7001), "http://192.0.2.7:8080", "not at a loopback address"),
			("a host name", dict(listener_pid=7001), "http://llama.example:8080", "not at a loopback address"),
			("no listener", dict(listener_pid=None), None, "no single process listens"),
			("another program listening", dict(listener_pid=7002), None, "not named llama-server*"),
		]
		for label, kw, url, why in cases:
			procs = {7001: ("llama-server.exe", 400), 7002: ("python.exe", 400)}
			info = {7001: {"exe": SERVER_EXE, "argv": [SERVER_EXE], "created": 1.0}, 7002: {"exe": "C:\\py\\python.exe", "argv": ["python.exe"], "created": 1.0}}
			with tempfile.TemporaryDirectory() as tmp, _patch_win(procs=procs, proc_info_map=info, proc_mem_map={}, **kw):
				c = state.Collector(_cfg(tmp, url or "http://127.0.0.1:18080"))
				self.assertIsNone(c.watched_exe(refresh=True), label)
				self.assertIn(why, c.trust_reason, label)
		with tempfile.TemporaryDirectory() as tmp, _patch_win(procs={7001: ("llama-server.exe", 400)}, proc_info_map={7001: {"exe": None, "argv": None, "created": None}}, proc_mem_map={}, listener_pid=7001):
			c = state.Collector(_cfg(tmp, "http://127.0.0.1:18080"))
			self.assertIsNone(c.watched_exe(refresh=True))
			self.assertIn("could not be read", c.trust_reason)
		with tempfile.TemporaryDirectory() as tmp, _trusted_win():  # the one that is trusted
			for url in ("http://127.0.0.1:18080", "http://localhost:18080", "http://[::1]:18080"):
				self.assertEqual(state.Collector(_cfg(tmp, url)).watched_exe(refresh=True), SERVER_EXE, url)

	def test_tcp_listener_pid_reports_the_listening_process(self):
		"""The call that decides which local server is trusted: a socket this test opens reports this test's pid."""
		with socket.socket() as s:
			s.bind(("127.0.0.1", 0))
			s.listen()
			port = s.getsockname()[1]
			self.assertEqual(win.tcp_listener_pid(port), os.getpid())
			self.assertEqual(win.tcp_listener_pid(port, "127.0.0.1"), os.getpid())
			self.assertEqual(win.tcp_listener_pid(port, "localhost"), os.getpid())
			self.assertIsNone(win.tcp_listener_pid(port, "::1"))  # nothing listens there
		self.assertIsNone(win.tcp_listener_pid(port))  # closed again
		for bad in (0, -1, 65536, None, "80", True):
			self.assertIsNone(win.tcp_listener_pid(bad))
		if socket.has_ipv6:
			try:
				with socket.socket(socket.AF_INET6) as s6:
					s6.bind(("::1", 0))
					s6.listen()
					port6 = s6.getsockname()[1]
					self.assertEqual(win.tcp_listener_pid(port6), os.getpid())
					self.assertEqual(win.tcp_listener_pid(port6, "::1"), os.getpid())
					self.assertIsNone(win.tcp_listener_pid(port6, "127.0.0.1"))
			except OSError:
				pass  # no IPv6 loopback on this machine

	def _run_cli(self, tmp, stub, *args, cwd=None, path_first=None):
		env = dict(os.environ)
		if path_first:
			env["PATH"] = path_first + os.pathsep + env.get("PATH", "")
		cmd = [sys.executable, "-I", LLMTOP_PY, *args, "--no-persist", "--router", stub.url, "--state-dir", os.path.join(tmp, "state")]
		return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, cwd=cwd, env=env)

	def test_a_program_in_the_current_folder_or_on_path_is_never_run(self):
		"""Old code resolved the server's name through the current folder and PATH and ran `<name> --help` there."""
		with tempfile.TemporaryDirectory() as tmp:
			decoy_dir = os.path.join(tmp, "decoy")
			_batch(os.path.join(decoy_dir, "llama-server.bat"), "decoy_ran.txt")
			os.makedirs(os.path.join(tmp, "elsewhere"))
			stub = _Stub(_status_models(["llama-server.bat", "--model", "C:\\x\\m.gguf"]))
			try:
				for kw in (dict(cwd=decoy_dir), dict(cwd=os.path.join(tmp, "elsewhere"), path_first=decoy_dir)):
					proc = self._run_cli(tmp, stub, "--once", "--page", "memory", **kw)
					self.assertEqual(proc.returncode, 0, proc.stderr)
					self.assertNotIn("traceback", (proc.stdout + proc.stderr).lower())
					self.assertIn("could not analyse", proc.stdout)
					self.assertFalse(os.path.exists(os.path.join(decoy_dir, "decoy_ran.txt")), kw)
			finally:
				stub.close()

	def test_check_says_in_plain_words_that_a_feature_is_off(self):
		"""--check names the reason, in the llama-fit-params line and as a notice; it used to look on PATH and in every server-supplied folder."""
		with tempfile.TemporaryDirectory() as tmp:
			stub = _Stub(_status_models(["C:\\elsewhere\\llama-server.exe", "--model", "C:\\x\\m.gguf"]))
			try:
				proc = self._run_cli(tmp, stub, "--check")
			finally:
				stub.close()
			self.assertNotIn("traceback", (proc.stdout + proc.stderr).lower())
			lines = {ln.split()[0]: ln for ln in proc.stdout.splitlines() if ln.strip()}
			self.assertIn("off: no helper program is run and no file path from the server is used, because", lines["llama-fit-params"])
			self.assertIn("not named llama-server*", lines["llama-fit-params"])
			self.assertTrue(any(ln.startswith("notice") and "no helper program is run" in ln for ln in proc.stdout.splitlines()), proc.stdout)


CASES = [
	("args0_of_an_untrusted_server_starts_nothing", TestTrust("test_args0_of_an_untrusted_server_starts_nothing")),
	("trusted_server_runs_only_its_own_help_and_never_args0", TestTrust("test_trusted_server_runs_only_its_own_help_and_never_args0")),
	("fit_projection_looks_only_in_the_servers_folder", TestTrust("test_fit_projection_looks_only_in_the_servers_folder")),
	("server_defaults_runs_only_an_absolute_existing_server_path", TestTrust("test_server_defaults_runs_only_an_absolute_existing_server_path")),
	("redirect_is_an_error_and_never_carries_the_key", TestTrust("test_redirect_is_an_error_and_never_carries_the_key")),
	("default_probe_sends_no_key_and_an_explicit_router_does", TestTrust("test_default_probe_sends_no_key_and_an_explicit_router_does")),
	("key_goes_to_a_url_matched_to_a_running_llama_server", TestTrust("test_key_goes_to_a_url_matched_to_a_running_llama_server")),
	("local_path_rule", TestTrust("test_local_path_rule")),
	("loopback_servers_bypass_the_proxy_and_remote_ones_use_it", TestTrust("test_loopback_servers_bypass_the_proxy_and_remote_ones_use_it")),
	("opener_is_built_per_request_from_the_current_environment", TestTrust("test_opener_is_built_per_request_from_the_current_environment")),
	("untrusted_paths_are_skipped_with_a_notice_never_an_exception", TestTrust("test_untrusted_paths_are_skipped_with_a_notice_never_an_exception")),
	("analysis_gets_only_vetted_flags_and_the_trusted_program", TestTrust("test_analysis_gets_only_vetted_flags_and_the_trusted_program")),
	("a_server_that_is_not_on_loopback_or_not_llama_server_is_not_trusted", TestTrust("test_a_server_that_is_not_on_loopback_or_not_llama_server_is_not_trusted")),
	("tcp_listener_pid_reports_the_listening_process", TestTrust("test_tcp_listener_pid_reports_the_listening_process")),
	("a_program_in_the_current_folder_or_on_path_is_never_run", TestTrust("test_a_program_in_the_current_folder_or_on_path_is_never_run")),
	("check_says_in_plain_words_that_a_feature_is_off", TestTrust("test_check_says_in_plain_words_that_a_feature_is_off")),
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
