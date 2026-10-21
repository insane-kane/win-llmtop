#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end test for win-llmtop against a real llama-server process.

	python -I tools/win-llmtop/tests/e2e_server.py --server <llama-server.exe> --model <file.gguf>
"""

import argparse
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from llmtop_lib import win  # noqa: E402

LLMTOP_PY = str(ROOT / "llmtop.py")

RE_REQ_ROW = re.compile(
	r"^\s*\d{2}:\d{2}:\d{2}\s+\d+\s+\d+\s+\d+\s+([0-9]+(?:\.[0-9]+)?)\s+t/s\s+\d+\s+([0-9]+(?:\.[0-9]+)?)\s+t/s\b"
)


def _free_port():
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
		s.bind(("localhost", 0))
		return s.getsockname()[1]


def _wait_health(base_url, proc, timeout=45.0):
	deadline = time.monotonic() + timeout
	last_err = None
	while time.monotonic() < deadline:
		if proc.poll() is not None:
			raise RuntimeError("llama-server exited early with code %s" % proc.returncode)
		try:
			req = urllib.request.Request(base_url + "/health")
			with urllib.request.urlopen(req, timeout=1.5) as resp:
				if resp.status == 200:
					data = json.loads(resp.read().decode("utf-8", errors="replace"))
					if data.get("status") == "ok":
						return
		except Exception as e:
			last_err = e
		time.sleep(0.25)
	raise RuntimeError("timed out waiting for %s/health (last error: %s)" % (base_url, last_err))


def _post_completion(base_url, prompt, n_predict=8, seed=42):
	payload = json.dumps({
		"prompt": prompt,
		"n_predict": n_predict,
		"seed": seed,
		"temperature": 0.0,
	}).encode("utf-8")
	req = urllib.request.Request(
		base_url + "/completion",
		data=payload,
		headers={"Content-Type": "application/json"},
		method="POST",
	)
	with urllib.request.urlopen(req, timeout=30.0) as resp:
		if resp.status != 200:
			raise RuntimeError("/completion returned status %d" % resp.status)
		return json.loads(resp.read().decode("utf-8", errors="replace"))


def _start_server(server_exe, model_path, port, log_file, extra_args=()):
	cmd = [
		server_exe,
		"--model", model_path,
		"--host", "localhost",
		"--port", str(port),
		"--slots",
		"--log-file", log_file,
		"--log-timestamps",
		"--ctx-size", "256",
		"--threads", "2",
		*extra_args,
	]
	creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
	startupinfo = None
	if hasattr(subprocess, "STARTUPINFO"):
		startupinfo = subprocess.STARTUPINFO()
		startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
		startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
	return subprocess.Popen(
		cmd,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		creationflags=creationflags,
		startupinfo=startupinfo,
	)


def _stop_server(proc):
	if proc is None or proc.poll() is not None:
		return
	proc.terminate()
	try:
		proc.wait(timeout=10)
	except subprocess.TimeoutExpired:
		proc.kill()
		proc.wait(timeout=5)


def _other_llama_pids(own_pid):
	if not win.WIN:
		return []
	return sorted(pid for pid, (exe, _) in win.processes().items() if exe.startswith("llama-server") and pid != own_pid)


def _run_llmtop(args, state_dir, artifact_dir=None, label=None):
	env = dict(os.environ)
	for k in ("LLMTOP_ROUTER", "LLMTOP_LOG_DIR", "LLMTOP_API_KEY", "LLAMA_API_KEY"):
		env.pop(k, None)
	cmd = [
		sys.executable, "-I", LLMTOP_PY,
		"--no-persist", "--no-color",
		"--state-dir", state_dir,
		*args,
	]
	proc = subprocess.run(
		cmd,
		capture_output=True,
		text=True,
		encoding="utf-8",
		errors="replace",
		env=env,
		timeout=45,
	)
	if artifact_dir and label:
		os.makedirs(artifact_dir, exist_ok=True)
		out_file = os.path.join(artifact_dir, "%s.txt" % label)
		with open(out_file, "w", encoding="utf-8") as f:
			f.write("COMMAND: %s\nRETURN CODE: %d\n--- STDOUT ---\n%s\n--- STDERR ---\n%s\n" % (
				" ".join(cmd), proc.returncode, proc.stdout, proc.stderr
			))
	return proc


def _check_case_check(proc, port, model_name, log_file, explicit_router):
	combined = (proc.stdout + proc.stderr).lower()
	if "traceback" in combined:
		return False, "traceback in --check output"
	if proc.returncode != 0:
		return False, "--check exited with %d (output: %s)" % (proc.returncode, proc.stdout.strip())
	lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
	server_line = next((ln for ln in lines if ln.startswith("server")), None)
	if not server_line:
		return False, "missing server line in --check output"
	how_marker = "(explicit)" if explicit_router else "from llama-server (pid "
	for marker in ("ok", "http://localhost:%d" % port, how_marker, "single mode", "1 model reported", "/slots reachable"):
		if marker not in server_line:
			return False, "server line missing %r: %s" % (marker, server_line)
	logs_line = next((ln for ln in lines if ln.startswith("logs")), None)
	if not logs_line:
		return False, "missing logs line in --check output"
	for marker in ("ok", model_name, log_file, "exists, timestamps"):
		if marker not in logs_line:
			return False, "logs line missing %r: %s" % (marker, logs_line)
	if "router log:" in logs_line:
		return False, "single-model logs line unexpectedly included a router log: %s" % logs_line
	return True, "server on localhost:%d (%s, single mode, 1 model, /slots reachable) and log verified with timestamps" % (
		port, "explicit" if explicit_router else "discovered"
	)


def _check_case_once_all(proc):
	combined = (proc.stdout + proc.stderr).lower()
	if "traceback" in combined or "render error" in combined:
		return False, "traceback or render error in --once --page all"
	if proc.returncode != 0:
		return False, "--once --page all exited with %d" % proc.returncode
	in_requests = False
	good_rows = []
	for ln in proc.stdout.splitlines():
		if "recent requests" in ln:
			in_requests = True
			continue
		if in_requests and ln.startswith("── "):
			in_requests = False
		if in_requests:
			m = RE_REQ_ROW.match(ln)
			if m:
				pp = float(m.group(1))
				tg = float(m.group(2))
				if pp > 0 and tg > 0:
					good_rows.append((pp, tg))
	if len(good_rows) < 2:
		return False, "expected >= 2 completed request rows with non-zero prefill/decode t/s, found %d" % len(good_rows)
	return True, "%d request rows rendered with non-zero speeds (%s)" % (
		len(good_rows), ", ".join("pp=%.1f tg=%.1f" % r for r in good_rows[:2])
	)


def _check_case_once_live(proc, model_name):
	combined = (proc.stdout + proc.stderr).lower()
	if "traceback" in combined or "render error" in combined:
		return False, "traceback or render error in --once --page live"
	if proc.returncode != 0:
		return False, "--once --page live exited with %d" % proc.returncode
	if model_name not in proc.stdout:
		return False, "model %r not shown on live page" % model_name
	if "single-model mode" not in proc.stdout:
		return False, "'single-model mode' notice missing on live page"
	return True, "model %s shown on live page in single-model mode" % model_name


def _check_case_unauthorized_reported(check_proc, live_proc, port_auth):
	for label, proc in (("--check", check_proc), ("--once --page live", live_proc)):
		combined = (proc.stdout + proc.stderr).lower()
		if "traceback" in combined or "render error" in combined:
			return False, "crash/traceback in %s with --api-key wrong" % label
	if check_proc.returncode != 1:
		return False, "--check with --api-key wrong exited %d (expected 1)" % check_proc.returncode
	if live_proc.returncode != 0:
		return False, "--once --page live with --api-key wrong exited %d" % live_proc.returncode
	check_lines = [ln.strip() for ln in check_proc.stdout.splitlines() if ln.strip()]
	server_line = next((ln for ln in check_lines if ln.startswith("server")), "")
	for marker in ("FAIL", "http://localhost:%d" % port_auth, "HTTP 401", "--api-key", "LLMTOP_API_KEY", "LLAMA_API_KEY"):
		if marker not in server_line:
			return False, "--check server line missing %r: %s" % (marker, server_line)
	if "unreachable" in server_line.lower():
		return False, "--check reported 'unreachable' instead of auth error: %s" % server_line
	for marker in ("HTTP 401", "--api-key", "LLMTOP_API_KEY", "LLAMA_API_KEY"):
		if marker not in live_proc.stdout:
			return False, "--once --page live missing %r in alert output" % marker
	if "router unreachable" in live_proc.stdout.lower():
		return False, "--once --page live reported 'router unreachable' instead of auth alert"
	return True, "wrong --api-key reported HTTP 401 auth error (--api-key / LLMTOP_API_KEY / LLAMA_API_KEY) without crashing"


def main():
	ap = argparse.ArgumentParser(description="End-to-end test for win-llmtop against a real llama-server.")
	ap.add_argument("--server", required=True, help="path to llama-server.exe")
	ap.add_argument("--model", required=True, help="path to tiny .gguf model")
	ap.add_argument("--artifact-dir", default=os.environ.get("LLMTOP_E2E_ARTIFACT_DIR") or None,
		help="optional directory to save server logs and llmtop outputs for CI failure artifacts")
	args = ap.parse_args()

	server_exe = os.path.abspath(args.server)
	model_path = os.path.abspath(args.model)
	model_name = os.path.basename(model_path)
	if not os.path.isfile(server_exe):
		sys.exit("error: --server not found: %s" % server_exe)
	if not os.path.isfile(model_path):
		sys.exit("error: --model not found: %s" % model_path)

	results = []

	def record(name, ok, detail):
		results.append((name, ok, detail))
		print("%s  %-28s %s" % ("PASS" if ok else "FAIL", name, detail))

	with tempfile.TemporaryDirectory(prefix="win-llmtop-e2e-") as tmp:
		log_file = os.path.join(tmp, "model.log")
		state_dir = os.path.join(tmp, "state")
		port = _free_port()
		base_url = "http://localhost:%d" % port
		proc = _start_server(server_exe, model_path, port, log_file)
		try:
			_wait_health(base_url, proc)
			other_pids = _other_llama_pids(proc.pid)
			explicit_router = bool(other_pids)
			if explicit_router:
				print(
					"NOTE  other llama-server running (pids %s); passing --router %s explicitly (discovery not tested)"
					% (", ".join(str(p) for p in other_pids), base_url)
				)
			router_args = ["--router", base_url] if explicit_router else []

			_post_completion(base_url, "Once upon a time there was a little frog", n_predict=8, seed=1)
			_post_completion(base_url, "Once upon a time there was a little bird", n_predict=8, seed=2)
			time.sleep(0.4)

			p_check = _run_llmtop([*router_args, "--check"], state_dir, args.artifact_dir, "01-check")
			ok, detail = _check_case_check(p_check, port, model_name, log_file, explicit_router)
			record("check_discover_and_logs", ok, detail)

			p_all = _run_llmtop([*router_args, "--once", "--page", "all", "--width", "120"], state_dir, args.artifact_dir, "02-once-all")
			ok, detail = _check_case_once_all(p_all)
			record("once_page_all_requests", ok, detail)

			p_live = _run_llmtop([*router_args, "--once", "--page", "live", "--width", "120"], state_dir, args.artifact_dir, "03-once-live")
			ok, detail = _check_case_once_live(p_live, model_name)
			record("once_page_live_model", ok, detail)
		finally:
			_stop_server(proc)
			if args.artifact_dir and os.path.isfile(log_file):
				os.makedirs(args.artifact_dir, exist_ok=True)
				shutil.copy2(log_file, os.path.join(args.artifact_dir, "model.log"))

		# Second server start with --api-key to test --api-key wrong behavior cleanly.
		log_file_auth = os.path.join(tmp, "model_auth.log")
		state_dir_auth = os.path.join(tmp, "state_auth")
		port_auth = _free_port()
		base_url_auth = "http://localhost:%d" % port_auth
		proc_auth = _start_server(
			server_exe, model_path, port_auth, log_file_auth,
			extra_args=("--api-key", "e2e-expected-key"),
		)
		try:
			_wait_health(base_url_auth, proc_auth)
			other_pids_auth = _other_llama_pids(proc_auth.pid)
			router_args_auth = ["--router", base_url_auth] if other_pids_auth else []
			p_auth_check = _run_llmtop(
				[*router_args_auth, "--check", "--api-key", "wrong"],
				state_dir_auth, args.artifact_dir, "04-wrong-key-check",
			)
			p_auth_live = _run_llmtop(
				[*router_args_auth, "--once", "--page", "live", "--width", "120", "--api-key", "wrong"],
				state_dir_auth, args.artifact_dir, "05-wrong-key-live",
			)
			ok, detail = _check_case_unauthorized_reported(p_auth_check, p_auth_live, port_auth)
			record("wrong_api_key_auth_error", ok, detail)
		finally:
			_stop_server(proc_auth)
			if args.artifact_dir and os.path.isfile(log_file_auth):
				os.makedirs(args.artifact_dir, exist_ok=True)
				shutil.copy2(log_file_auth, os.path.join(args.artifact_dir, "model_auth.log"))

	passed = sum(1 for _, ok, _ in results if ok)
	total = len(results)
	if passed == total:
		print("ALL PASS (%d/%d)" % (passed, total))
		return 0
	print("FAILED (%d/%d passed)" % (passed, total))
	return 1


if __name__ == "__main__":
	sys.exit(main())
