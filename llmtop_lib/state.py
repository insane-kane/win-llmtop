# SPDX-License-Identifier: GPL-3.0-or-later
"""llmtop engine: time series, persistence, alert rules, and the per-tick collector that fuses every data source into one snapshot."""

import hashlib
import ipaddress
import json
import math
import os
import re
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from array import array
from collections import defaultdict, deque

from . import log as logmod
from . import model, win

NAN = float("nan")
MIB = 2**20
CFG = {
	"router": None,
	"log_dir": None,
	"router_log": None,
	"state_dir": os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local"), "win-llmtop"),
	"api_key": None,
	"gpu": None,
	"hz": 4.0,  # sampling and redraw rate
	"history_hours": 24,
	"persist": True,
	"calibrate": True,  # save the learned per-configuration VRAM overhead
	"spill_mib": 256,  # shared GPU memory held by llama-server before it counts as an overflow
	"burst_mib": 128,  # rise in shared GPU memory within burst_window_s that raises an alert
	"avail_warn": 4096,
	"avail_crit": 2048,
	"commit_warn": 8192,
	"commit_crit": 3072,
	"hold_s": 30,  # how long a burst stays in the banner
	"clear_s": 10,  # quiet time before a sustained alert clears
	"near_budget_mib": 512,  # shared-memory overflow is critical when resident VRAM is this close to the OS budget
	"at_budget_mib": 64,  # "within N of the budget" warning
	"idle_headroom_mib": 1024,  # resident plus shared memory this far under the OS budget counts as "no VRAM pressure" when learning the shared baseline
	"rival_gpu_pct": 30,  # another process using this share of the GPU while a request runs
	"card_full_pct": 97,
	"pagefile_pct": 50,
	"ws_drop_mib": 1024,  # fall in llama-server's working set within 10 s
	"burst_window_s": 4,  # window for the shared-memory rise
	"load_grace_s": 90,  # rises in shared memory this soon after a load are not reported as bursts
	"prompt_gap_s": 30,  # idle gap in seconds between turns on the same slot that starts a new prompt
}
SCALED_THRESHOLDS = ("avail_warn", "avail_crit", "commit_warn", "commit_crit", "near_budget_mib", "idle_headroom_mib")
RE_BAD_FILE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# series recorded once per second: key -> how the sub-second samples are combined
SERIES = {"vram": "max", "vram_llama": "max", "spill": "max", "budget": "last", "gpu_llama": "max", "gpu_copy": "max", "gpu_other": "max",
	"ram_used": "max", "ram_avail": "min", "commit": "max", "llama_ws": "max", "llama_cpu": "mean", "cpu": "mean", "cpu_mhz": "mean", "pages_in": "max", "ctx": "max",
	"dec": "mean", "pre": "mean", "busy": "max", "heap": "last", "pf_used": "max", "pages_out": "max", "standby": "mean", "free": "mean"}
LEVELS = {"info": 0, "warn": 1, "crit": 2}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
	"""Refuses every redirect: a 3xx answer is raised as an HTTPError, like any other HTTP error, and no second request is made."""

	def redirect_request(self, req, fp, code, msg, headers, newurl):
		raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def opener_for(url):
	"""The opener for a request to `url`: no redirect is followed, and a loopback server is reached directly (never through a proxy) while any other
	server goes through the system proxy settings. Built per request, so the environment and registry settings are read as they are now."""
	proxies = urllib.request.ProxyHandler({}) if is_loopback_host(urllib.parse.urlsplit(url).hostname) else urllib.request.ProxyHandler()
	return urllib.request.build_opener(proxies, _NoRedirect())


def is_loopback_host(host):
	"""True for `localhost` and for a loopback IP address (127.0.0.0/8, ::1); a host name is never looked up."""
	h = str(host or "").strip("[]").lower()
	if h == "localhost":
		return True
	try:
		return ipaddress.ip_address(h).is_loopback
	except ValueError:
		return False


def agg(xs, how):
	return max(xs) if how == "max" else min(xs) if how == "min" else sum(xs) / len(xs) if how == "mean" else xs[-1]


def write_json_atomic(path, obj, indent=None):
	"""Write `obj` as JSON to `path` through a temporary file in the same folder and os.replace, so `path` holds either the old or the new content, never a partial one."""
	tmp = path + ".tmp"
	try:
		with open(tmp, "w", encoding="utf-8") as f:
			json.dump(obj, f, indent=indent)
			f.flush()
			os.fsync(f.fileno())
		os.replace(tmp, path)
	except Exception:
		try:
			os.remove(tmp)
		except OSError:
			pass
		raise


def scale_thresholds(cfg, ram_mib=None, commit_max_mib=None, budget_mib=None, user_set=()):
	"""Scale memory and VRAM alert thresholds to fit the machine unless explicitly set by the user."""
	if ram_mib and ram_mib > 0:
		if "avail_warn" not in user_set:
			cfg["avail_warn"] = min(4096, round(0.07 * ram_mib))
		if "avail_crit" not in user_set:
			cfg["avail_crit"] = min(2048, round(0.035 * ram_mib))
	if commit_max_mib and commit_max_mib > 0:
		if "commit_warn" not in user_set:
			cfg["commit_warn"] = min(8192, round(0.10 * commit_max_mib))
		if "commit_crit" not in user_set:
			cfg["commit_crit"] = min(3072, round(0.04 * commit_max_mib))
	if budget_mib and budget_mib > 0:
		if "near_budget_mib" not in user_set:
			cfg["near_budget_mib"] = min(512, round(0.03 * budget_mib))
		if "idle_headroom_mib" not in user_set:
			cfg["idle_headroom_mib"] = min(1024, round(0.06 * budget_mib))
	return cfg


def _win_err(fn, pid):
	e = win.errors.get((fn, pid))
	if not e:
		return None
	if isinstance(e, tuple) and len(e) == 2:
		return "%s (error %s)" % (e[1], e[0]) if e[0] else str(e[1])
	return str(e)


class Ring:
	"""One value per wall-clock second over `span` seconds; NaN where nothing was recorded."""

	def __init__(self, span=24 * 3600, how="max"):
		self.span, self.how = span, how
		self.v, self.s = array("f", [NAN]) * span, array("q", [0]) * span
		self.span10, self.span60 = max(1, span // 10), max(1, span // 60)
		self.s10, self.s60 = array("q", [-1]) * self.span10, array("q", [-1]) * self.span60
		if how == "mean":
			self.sum10 = array("f", [0.0]) * self.span10
			self.cnt10 = array("h", [0]) * self.span10
			self.sum60 = array("f", [0.0]) * self.span60
			self.cnt60 = array("h", [0]) * self.span60
		else:
			self.v10 = array("f", [NAN]) * self.span10
			self.v60 = array("f", [NAN]) * self.span60
			if how == "last":
				self.l10 = array("q", [-1]) * self.span10
				self.l60 = array("q", [-1]) * self.span60

	def put(self, t, x):
		sec = int(t)
		isec = sec % self.span
		old_sec = self.s[isec]
		self.v[isec], self.s[isec] = x, sec
		if old_sec == sec:
			self._recompute(sec, 10)
			self._recompute(sec, 60)
			return

		# 10s
		b10 = sec // 10
		i10 = b10 % self.span10
		if self.s10[i10] != b10:
			self.s10[i10] = b10
			if self.how == "mean":
				self.sum10[i10], self.cnt10[i10] = x, 1
			elif self.how == "last":
				self.v10[i10], self.l10[i10] = x, sec
			else:
				self.v10[i10] = x
		else:
			if self.how == "max":
				if x > self.v10[i10]:
					self.v10[i10] = x
			elif self.how == "min":
				if x < self.v10[i10]:
					self.v10[i10] = x
			elif self.how == "mean":
				self.sum10[i10] += x
				self.cnt10[i10] += 1
			elif self.how == "last":
				if sec >= self.l10[i10]:
					self.v10[i10], self.l10[i10] = x, sec

		# 60s
		b60 = sec // 60
		i60 = b60 % self.span60
		if self.s60[i60] != b60:
			self.s60[i60] = b60
			if self.how == "mean":
				self.sum60[i60], self.cnt60[i60] = x, 1
			elif self.how == "last":
				self.v60[i60], self.l60[i60] = x, sec
			else:
				self.v60[i60] = x
		else:
			if self.how == "max":
				if x > self.v60[i60]:
					self.v60[i60] = x
			elif self.how == "min":
				if x < self.v60[i60]:
					self.v60[i60] = x
			elif self.how == "mean":
				self.sum60[i60] += x
				self.cnt60[i60] += 1
			elif self.how == "last":
				if sec >= self.l60[i60]:
					self.v60[i60], self.l60[i60] = x, sec

	def _recompute(self, sec, size):
		b = sec // size
		span_b = self.span10 if size == 10 else self.span60
		sb = self.s10 if size == 10 else self.s60
		ib = b % span_b
		start_s = b * size
		vals = []
		last_s = -1
		last_v = NAN
		for s in range(start_s, start_s + size):
			isec = s % self.span
			if self.s[isec] == s:
				v = self.v[isec]
				vals.append(v)
				last_s = s
				last_v = v
		if not vals:
			sb[ib] = -1
			return
		sb[ib] = b
		if self.how == "max":
			v_arr = self.v10 if size == 10 else self.v60
			v_arr[ib] = max(vals)
		elif self.how == "min":
			v_arr = self.v10 if size == 10 else self.v60
			v_arr[ib] = min(vals)
		elif self.how == "mean":
			sum_arr = self.sum10 if size == 10 else self.sum60
			cnt_arr = self.cnt10 if size == 10 else self.cnt60
			sum_arr[ib] = sum(vals)
			cnt_arr[ib] = len(vals)
		elif self.how == "last":
			v_arr = self.v10 if size == 10 else self.v60
			l_arr = self.l10 if size == 10 else self.l60
			v_arr[ib] = last_v
			l_arr[ib] = last_s

	def at(self, sec):
		i = sec % self.span
		return self.v[i] if self.s[i] == sec else NAN

	def cols(self, t_end, seconds, n, how=None):
		"""`n` columns covering the last `seconds` seconds up to `t_end`; NaN for gaps."""
		kind = self.how if how is None else how
		if kind == self.how:
			ratio = seconds / n
			size = 60 if ratio >= 120 else 10 if ratio >= 20 else 0
		else:
			size = 0

		if not size:
			start = int(t_end) + 1 - seconds
			out = []
			for j in range(n):
				a, b = start + seconds * j // n, start + seconds * (j + 1) // n
				xs = [x for x in map(self.at, range(a, max(b, a + 1))) if x == x]
				out.append(agg(xs, kind) if xs else NAN)
			return out

		span_b = self.span60 if size == 60 else self.span10
		sb = self.s60 if size == 60 else self.s10
		b_end = int(t_end) // size
		n_buckets = seconds // size
		b_start = b_end + 1 - n_buckets
		out = []
		if kind == "max":
			vb = self.v60 if size == 60 else self.v10
			for j in range(n):
				bj_a = b_start + n_buckets * j // n
				bj_b = b_start + n_buckets * (j + 1) // n
				m = -float("inf")
				found = False
				for b in range(bj_a, bj_b):
					ib = b % span_b
					if sb[ib] == b:
						v = vb[ib]
						if v > m:
							m = v
						found = True
				out.append(m if found else NAN)
		elif kind == "min":
			vb = self.v60 if size == 60 else self.v10
			for j in range(n):
				bj_a = b_start + n_buckets * j // n
				bj_b = b_start + n_buckets * (j + 1) // n
				m = float("inf")
				found = False
				for b in range(bj_a, bj_b):
					ib = b % span_b
					if sb[ib] == b:
						v = vb[ib]
						if v < m:
							m = v
						found = True
				out.append(m if found else NAN)
		elif kind == "mean":
			sum_b = self.sum60 if size == 60 else self.sum10
			cnt_b = self.cnt60 if size == 60 else self.cnt10
			for j in range(n):
				bj_a = b_start + n_buckets * j // n
				bj_b = b_start + n_buckets * (j + 1) // n
				tot_sum = 0.0
				tot_cnt = 0
				for b in range(bj_a, bj_b):
					ib = b % span_b
					if sb[ib] == b:
						tot_sum += sum_b[ib]
						tot_cnt += cnt_b[ib]
				out.append(tot_sum / tot_cnt if tot_cnt > 0 else NAN)
		elif kind == "last":
			vb = self.v60 if size == 60 else self.v10
			for j in range(n):
				bj_a = b_start + n_buckets * j // n
				bj_b = b_start + n_buckets * (j + 1) // n
				v_last = NAN
				for b in range(bj_b - 1, bj_a - 1, -1):
					ib = b % span_b
					if sb[ib] == b:
						v_last = vb[ib]
						break
				out.append(v_last)
		return out

	def span_max(self, t0, t1):
		xs = [x for x in map(self.at, range(int(t0), int(t1) + 1)) if x == x]
		return max(xs) if xs else NAN


class History:
	"""JSONL on disk: one sample line per 5 s plus alert events, so sparklines and the event list survive a restart."""

	def __init__(self, directory, keep_days=14, on_error=None):
		self.dir, self.f, self.day, self.on_error = directory, None, None, on_error
		if directory and os.path.isdir(directory):
			try:
				for name in os.listdir(directory):
					if name.startswith("samples-") and name.endswith(".jsonl") and time.time() - os.path.getmtime(os.path.join(directory, name)) > keep_days * 86400:
						try:
							os.remove(os.path.join(directory, name))
						except OSError:
							pass
			except OSError:
				pass

	def _write(self, t, obj):
		if not self.dir:
			return
		day = time.strftime("%Y%m%d", time.localtime(t))
		try:
			os.makedirs(self.dir, exist_ok=True)
			if day != self.day or self.f is None:
				if self.f:
					self.f.close()
				self.f, self.day = open(os.path.join(self.dir, "samples-%s.jsonl" % day), "a", encoding="utf-8"), day
			self.f.write(json.dumps(obj, separators=(",", ":")) + "\n")
			self.f.flush()
		except (OSError, ValueError) as e:
			if self.f:
				try:
					self.f.close()
				except OSError:
					pass
				self.f = None
			if self.on_error:
				self.on_error(e)

	def write_sample(self, sec, vals):
		self._write(sec, {"t": sec, "v": {k: round(v, 1) for k, v in vals.items()}})

	def write_event(self, ev):
		e = {k: ev[k] for k in ("level", "kind", "msg")}
		if ev.get("detail"):
			e["detail"] = ev["detail"]
		self._write(ev["wall"], {"t": int(ev["wall"]), "e": e})

	def load(self, since):
		"""(samples, events) recorded after `since` (epoch)."""
		if not self.dir or not os.path.isdir(self.dir):
			return [], []
		samples, events = [], []
		try:
			names = sorted(os.listdir(self.dir))
		except OSError:
			return [], []
		for name in names:
			if not (name.startswith("samples-") and name.endswith(".jsonl")):
				continue
			try:
				with open(os.path.join(self.dir, name), encoding="utf-8") as f:
					for line in f:
						try:
							d = json.loads(line)
						except ValueError:
							continue
						if d.get("t", 0) < since:
							continue
						if "v" in d:
							samples.append((d["t"], d["v"]))
						elif "e" in d:
							events.append({"wall": d["t"], "t": 0, "preset": None, "session": None, **d["e"]})
			except OSError:
				continue
		return samples, events

	def close(self):
		if self.f:
			try:
				self.f.close()
			except OSError:
				pass
			self.f = None


class RequestLog:
	"""Finished requests, one JSON line each. llama-server rewrites a preset's log file every time that model loads, so the logs alone lose history."""

	def __init__(self, directory, on_error=None, persist=False):
		self.path = os.path.join(directory, "requests.jsonl") if directory else None
		self.on_error = on_error
		self.persist = persist

	def load(self, limit=50000):
		if not self.path or not os.path.isfile(self.path):
			return []
		out = []
		lines = 0
		ok = True
		try:
			with open(self.path, encoding="utf-8") as f:
				for line in f:
					lines += 1
					try:
						out.append(json.loads(line))
					except ValueError:
						continue
		except OSError:
			ok = False
		if ok and self.persist and lines > 2 * limit:
			self._trim(out[-limit:])
		return out[-limit:]

	def _trim(self, keep):
		"""Rewrite the file so it holds only the newest lines; a failure leaves it as it was."""
		tmp = self.path + ".tmp"
		try:
			with open(tmp, "w", encoding="utf-8") as f:
				for r in keep:
					f.write(json.dumps(r, separators=(",", ":")) + "\n")
			os.replace(tmp, self.path)
		except OSError:
			try:
				os.remove(tmp)
			except OSError:
				pass

	def append(self, r):
		if not self.path:
			return
		try:
			os.makedirs(os.path.dirname(self.path), exist_ok=True)
			with open(self.path, "a", encoding="utf-8") as f:
				f.write(json.dumps({k: v for k, v in r.items() if k not in ("spill", "rival")}, separators=(",", ":")) + "\n")
		except (OSError, ValueError, TypeError) as e:
			if self.on_error:
				self.on_error(e)


class Recorder:
	"""Folds the sub-second samples into one value per second per series."""

	def __init__(self, rings, hist=None):
		self.rings, self.hist, self.sec, self.acc, self.n = rings, hist, None, {}, 0

	def add(self, t, vals):
		sec = int(t)
		if self.sec is not None and sec != self.sec:
			self.flush()
		self.sec = sec
		for k, x in vals.items():
			if x == x:
				self.acc.setdefault(k, []).append(x)

	def flush(self):
		out = {k: agg(xs, SERIES[k]) for k, xs in self.acc.items()}
		for k, x in out.items():
			self.rings[k].put(self.sec, x)
		self.acc, self.n = {}, self.n + 1
		if self.hist and out and self.n % 5 == 0:
			self.hist.write_sample(self.sec, out)


class Alerts:
	"""Alert rules with hysteresis. A rule refreshes its alert while its condition holds; it clears after CFG['clear_s'] of quiet."""

	def __init__(self, cfg, emit):
		self.cfg, self.emit, self.active, self.prev = cfg, emit, {}, deque(maxlen=40)

	def flag(self, key, level, msg, now, hold=0, detail=None):
		a = self.active.get(key)
		if a is None:
			self.active[key] = {"level": level, "msg": msg, "detail": detail, "since": now, "last": now + hold}
			self.emit(level, "alert", msg, detail=detail)
		else:
			a.update(level=level, msg=msg, detail=detail, last=max(a["last"], now + hold))

	def sweep(self, now):
		for key in [k for k, a in self.active.items() if now - a["last"] > self.cfg["clear_s"]]:
			a = self.active.pop(key)
			self.emit("info", "alert", "cleared after %s: %s" % (hms(now - a["since"]), a["msg"]))

	def ordered(self):
		return sorted(self.active.items(), key=lambda kv: (-LEVELS[kv[1]["level"]], kv[1]["since"]))

	def evaluate(self, snap, mem):
		"""Every alert is a short headline (msg) plus an optional detail; the detail carries the comparisons, thresholds and deductions."""
		now, c, g, ll, rm = snap["t"], self.cfg, snap["gpu"], snap["llama"], snap["ram"]
		sent = snap.get("router_key_allowed", True)  # False: the default address is probed without the API key
		if not snap["router_up"]:
			if snap.get("router_auth_error"):
				self.flag("router", "crit", headline("%s: pass %s" % (snap["router_auth_error"], "--api-key or set LLMTOP_API_KEY / LLAMA_API_KEY" if sent else "--router with --api-key or LLAMA_API_KEY")), now,
					detail="server at %s returned %s" % (c["router"], snap["router_auth_error"]))
			else:
				self.flag("router", "crit", headline("router unreachable at %s" % c["router"]), now, detail=snap["router_error"] or None)
		for preset, p in snap["presets"].items():
			err = p.get("slot_error")
			if err:
				fix = (("pass --api-key or set LLAMA_API_KEY" if sent else "pass --router with --api-key or LLAMA_API_KEY") if err in ("HTTP 401", "HTTP 403") else
					"the server's /slots endpoint is off (start it with --slots)" if err in ("HTTP 404", "HTTP 501") else None)
				self.flag("slots:" + preset, "info", headline("/slots for %s returned %s" % (preset, err)), now, detail=fix)
			else:
				self.active.pop("slots:" + preset, None)
		if g and ll["present"]:
			self.prev.append((now, ll["shr"], ll["ded"]))
			old = next((p for p in self.prev if now - p[0] <= c["burst_window_s"]), None)
			near = bool(g["budget"]) and ll["ded"] >= g["budget"] - c["near_budget_mib"]
			where = "within %s of the OS budget %s, the condition under which Windows documents demoting allocations" % (mib(c["near_budget_mib"]), mib(g["budget"])) if near else "%s under the OS budget %s" % (mib(g["budget"] - ll["ded"]), mib(g["budget"]))
			grew = ll["floor_known"] and ll["shr"] - ll["floor"] >= c["spill_mib"]
			excess = ll["allow_known"] and ll["over"] >= c["spill_mib"]
			if grew or excess:
				facts = []
				if ll["floor_known"]:
					facts.append("%s the %s it held once the model was resident (lowest seen since)" % ("up %s from" % mib(ll["shr"] - ll["floor"]) if ll["shr"] - ll["floor"] >= 1 else "unchanged from", mib(ll["floor"])))
				if ll["allow_known"]:
					src = "measured with no VRAM pressure" if ll["allow_src"] == "measured" else "projected by llama.cpp"
					facts.append("expected %s (%s)" % (mib(ll["allow"]), src))
				if ll["allow_known"]:
					msg = "shared %s, %s beyond expected" % (mib(ll["shr"]), mib(ll["over"]))
				else:
					msg = "shared %s, %s above the resident minimum" % (mib(ll["shr"]), mib(ll["shr"] - ll["floor"]))
				self.flag("spill", "warn" if grew else "info", headline(msg), now,
					detail="%s. Resident VRAM %s is %s" % ("; ".join(facts), mib(ll["ded"]), where))
			if old and ll["age"] > c["load_grace_s"] and ll["shr"] - old[1] >= c["burst_mib"]:
				rose, fell = ll["shr"] - old[1], old[2] - ll["ded"]
				if fell >= 0.5 * rose:
					self.flag("burst", "crit", headline("memory moved from VRAM to shared: +%s in %.0f s" % (mib(rose), now - old[0])), now, hold=c["hold_s"],
						detail="shared rose by %s and resident VRAM fell by %s within %.0f s (total %+.0f MiB)" % (mib(rose), mib(fell), now - old[0], rose - fell))
				else:
					self.flag("burst", "warn", headline("shared memory rose %s in %.0f s" % (mib(rose), now - old[0])), now, hold=c["hold_s"],
						detail="without a matching fall in resident VRAM (resident %+.0f MiB)" % (ll["ded"] - old[2]))
			if old and old[1] - ll["shr"] >= c["burst_mib"]:
				self.flag("return", "info", headline("shared memory fell %s in %.0f s" % (mib(old[1] - ll["shr"]), now - old[0])), now, hold=c["hold_s"] / 2,
					detail="resident VRAM %+.0f MiB" % (ll["ded"] - old[2]))
			if ll["com"] - ll["ded"] - ll["shr"] >= c["spill_mib"]:
				self.flag("paged", "info" if ll["age"] < c["load_grace_s"] else "crit",
					headline("%s of committed GPU memory not in VRAM or shared" % mib(ll["com"] - ll["ded"] - ll["shr"])), now,
					detail="committed %s, resident %s, shared %s" % (mib(ll["com"]), mib(ll["ded"]), mib(ll["shr"])))
			if g["budget"] and ll["ded"] >= g["budget"] - c["at_budget_mib"] and ll["over"] < c["spill_mib"]:
				self.flag("budget", "warn", headline("resident VRAM %s, within %s of the OS budget" % (mib(ll["ded"]), mib(c["at_budget_mib"]))), now,
					detail="OS budget %s" % mib(g["budget"]))
			rival = sorted((o for o in snap["others"] if o["eng"] >= c["rival_gpu_pct"]), key=lambda o: -o["eng"])
			if rival and snap["busy"]:
				self.flag("rival", "warn", headline("%s is using %.0f%% of the GPU" % (rival[0]["exe"], rival[0]["eng"])), now,
					detail="while a llama-server request is running")
			if not g.get("uma") and g["vram"] > 0 and g["ded"] >= c["card_full_pct"] / 100.0 * g["vram"] and ll["over"] < c["spill_mib"]:
				self.flag("full", "warn", headline("GPU memory is %.0f%% used" % (100 * g["ded"] / g["vram"])), now,
					detail="%s of %s" % (mib(g["ded"]), mib(g["vram"])))
		if rm:
			if rm["avail"] < c["avail_crit"]:
				self.flag("ram", "crit", headline("available RAM %s" % mib(rm["avail"])), now)
			elif rm["avail"] < c["avail_warn"]:
				self.flag("ram", "warn", headline("available RAM %s" % mib(rm["avail"])), now)
			room, room_now = rm["commit_max"] - rm["commit"], rm["commit_limit"] - rm["commit"]
			if room < c["commit_crit"]:
				self.flag("commit", "crit", headline("commit charge %s, %s before the limit" % (mib(rm["commit"]), mib(room))), now,
					detail="page file at its configured maximum (%s); deduced: Windows fails commit requests beyond the limit" % mib(rm["commit_max"]))
			elif room < c["commit_warn"]:
				self.flag("commit", "warn", headline("commit charge %s, %s before the limit" % (mib(rm["commit"]), mib(room))), now,
					detail="page file at its configured maximum (%s)" % mib(rm["commit_max"]))
			elif room_now < c["commit_warn"]:
				self.flag("commit", "info", headline("commit charge %s, %s under the current limit" % (mib(rm["commit"]), mib(room_now))), now,
					detail="current commit limit %s; the page file is below its configured maximum" % mib(rm["commit_limit"]))
			for pf in rm["pagefiles"]:
				if pf["size"] and pf["used"] / pf["size"] * 100 >= c["pagefile_pct"]:
					self.flag("pagefile", "warn", headline("page file %s is %.0f%% used" % (basename(pf["name"]), 100 * pf["used"] / pf["size"])), now,
						detail="%s of %s (%s)" % (mib(pf["used"]), mib(pf["size"]), pf["name"]))
		if ll["present"] and ll["ws_drop"] >= c["ws_drop_mib"]:
			if ll["commit_drop"] >= 0.5 * ll["ws_drop"]:
				self.flag("trim", "info", headline("working set fell %s, commit fell %s" % (mib(ll["ws_drop"]), mib(ll["commit_drop"]))), now, hold=c["hold_s"],
					detail="the process released memory (deduced: commit only falls when memory is released)")
			else:
				self.flag("trim", "warn", headline("working set fell %s, commit did not" % mib(ll["ws_drop"])), now, hold=c["hold_s"],
					detail="commit fell by only %s: the pages left the working set and stayed committed (deduced: Windows reclaimed them, llama-server did not free them)" % mib(ll["commit_drop"]))
		for ev in snap["new_events"]:
			if ev["kind"] == "exit" and ev["level"] == "crit":
				self.flag("exit", "crit", ev["msg"], now, hold=60, detail=ev.get("detail"))
		self.sweep(now)


class Collector:
	"""Polls every source once per tick and keeps the derived state (request history, events, memory maps)."""

	def __init__(self, cfg=CFG):
		if cfg is CFG:
			cfg = dict(CFG)
		else:
			for k, v in CFG.items():
				cfg.setdefault(k, v)
		self.cfg = cfg
		self.user_thresholds = set(cfg.get("user_thresholds") or ())
		for k in SCALED_THRESHOLDS:
			if cfg.get(k) != CFG[k]:
				self.user_thresholds.add(k)
		raw_router = cfg.get("router")
		self.router_explicit = bool(raw_router)
		self.cfg["router"] = (raw_router or "http://127.0.0.1:8080").rstrip("/")
		self.router_discovery, self.router_tried, self.mode = None, [], None
		self._discover_idx = 0
		self._discover_tried = []
		self._alias_cache = {}
		self._alias_threads = {}
		self._alias_lock = threading.Lock()
		self._last_api_key = self.cfg.get("api_key")
		self.router_keyed = False  # True once the URL in use was taken from a running llama-server's command line; the API key goes only to such a URL or one the user gave
		self._trust, self.trust_reason, self.rejected = None, "", {}  # the trusted server (see watched_exe), why there is none, and the server-supplied paths refused: {(preset, flag): notice}
		self.dx = win.Dxgi() if win.WIN else None
		self.gpu = self.dx.main() if self.dx else None
		self.gpu_notice = None
		self.counters = win.Counters() if win.WIN else None
		self.procs, self.pids, self.pinfo, self.due, self.prev = {}, (), {}, {}, {}
		self.pagefile_max = win.pagefile_max() if win.WIN else None
		sm = win.sys_mem() if win.WIN else {}
		commit_max = (max(sm["commit_limit"], sm["total"] + self.pagefile_max) if self.pagefile_max else sm.get("commit_limit")) if sm else None
		scale_thresholds(self.cfg, sm.get("total") if sm else None, commit_max, self._gpu_budget(self.gpu), self.user_thresholds)
		self.state_dir = self.cfg.get("state_dir")
		self.state_error = None
		if self.cfg.get("persist") or self.cfg.get("calibrate"):
			self._validate_state_dir()
		self.rings = {k: Ring(self.cfg["history_hours"] * 3600, SERIES[k]) for k in SERIES}
		self.hist = History(self.state_dir if not self.state_error else None, on_error=self._fail_state_dir)
		self.reqlog = RequestLog(self.state_dir if not self.state_error else None, on_error=self._fail_state_dir, persist=self.cfg["persist"])
		self.rec = Recorder(self.rings, self.hist if self.cfg["persist"] else None)
		self.events, self.requests, self.follow, self.router_tail, self.router_start = [], [], {}, None, None
		self.req_seq = 0
		self.models, self.flags, self.mem, self.rate = {}, {}, {}, {}
		self.router_up, self.router_error, self.router_auth_error, self.last = False, "", None, None
		self.ws_hist = deque(maxlen=48)
		self.calib, self.calib_pid = {}, {}
		self.allow, self.allow_known, self.pid_seen, self.shr_floor = 0.0, False, {}, {}
		self.vm, self.vm_error, self.vm_busy, self.ws_by, self.ws_total, self.pagefiles = None, None, False, {}, 0.0, []
		self.pf_cfg = win.pagefile_config() if win.WIN else []
		self.cpu_info = win.cpu_info() if win.WIN else {"name": "", "cores": 0, "threads": 0, "p_cores": 0, "e_cores": 0, "caches": [], "error": "not Windows"}
		self.calib_file = os.path.join(self.state_dir, "calibration.json") if self.state_dir and not self.state_error else None
		if self.calib_file and os.path.isfile(self.calib_file):
			try:
				with open(self.calib_file, encoding="utf-8") as f:
					self.calib = {k: v for k, v in json.load(f).items() if isinstance(v, dict) and v.get("v") == 4}  # older files held baselines learned under a headroom test on resident memory alone
			except (OSError, ValueError):
				pass
		self.fit_cache_file = os.path.join(self.state_dir, "fit-cache.json") if self.state_dir and not self.state_error else None
		self.fit_cache = {}
		if self.fit_cache_file and os.path.isfile(self.fit_cache_file):
			try:
				with open(self.fit_cache_file, encoding="utf-8") as f:
					cached = json.load(f)
				if isinstance(cached, dict):
					self.fit_cache = cached
			except (OSError, ValueError):
				pass
		self._fit_lock = threading.Lock()
		self.alerts = Alerts(self.cfg, self.emit)
		self.started = time.time()

	@staticmethod
	def _gpu_budget(g):
		if not g:
			return 0.0
		if g.get("vram", 0.0) <= 0:
			return g.get("budget_shared") or g.get("shared") or g.get("budget", 0.0)
		return g.get("budget", 0.0)

	def _fail_state_dir(self, e):
		if not self.state_error:
			self.state_error = str(e)
		self.cfg["persist"] = False
		self.cfg["calibrate"] = False
		if hasattr(self, "rec") and self.rec:
			self.rec.hist = None

	def _validate_state_dir(self):
		d = self.state_dir
		if not d or not isinstance(d, (str, os.PathLike)):
			self._fail_state_dir("no state directory configured")
			return
		s = os.fspath(d)
		drive, tail = os.path.splitdrive(s)
		if any(c in tail for c in '\x00*?"<>|:'):
			self._fail_state_dir("invalid path %r" % s)
			return
		if drive and not os.path.exists(drive + os.sep):
			self._fail_state_dir("drive %s does not exist" % drive)
			return
		try:
			cur = os.path.abspath(s)
			while cur:
				if os.path.exists(cur):
					if not os.path.isdir(cur):
						self._fail_state_dir("not a directory: %s" % cur)
						return
					if not os.access(cur, os.W_OK):
						self._fail_state_dir("permission denied: %s" % cur)
						return
					break
				parent = os.path.dirname(cur)
				if parent == cur:
					break
				cur = parent
		except (OSError, ValueError) as e:
			self._fail_state_dir(e)

	def _proc_info(self, pid):
		if not pid or not win.WIN:
			return {"exe": None, "argv": None, "created": None}
		info = self.pinfo.get(pid)
		if info is None:
			info = win.proc_info(pid)
			self.pinfo[pid] = info
		return info

	def _update_procs(self, now):
		if not win.WIN:
			return
		self.procs = win.processes()
		if self.dx:
			self.dx.refresh()
		pids = tuple(sorted(p for p, v in self.procs.items() if v[0].startswith("llama-server")))
		for dead in [p for p in self.pinfo if p not in pids]:
			self.pinfo.pop(dead, None)
		if pids != self.pids or self.is_due("rebuild", now, 20.0):
			self.pids = pids
			if self.counters:
				self.counters.rebuild()

	def _independent_pids(self):
		kids = {p for p in self.pids if self.procs.get(self.procs.get(p, ("", 0))[1], ("",))[0].startswith("llama-server")}
		return [p for p in self.pids if p not in kids]

	def _pid_url_and_flags(self, pid):
		info = self._proc_info(pid)
		argv, exe = info.get("argv"), info.get("exe")
		watched = self.watched_exe()
		if not argv:
			return None, None, (({}, watched) if exe else None)
		pflags = model.parse_args(argv, self._aliases())
		host = pflags.get("host", "127.0.0.1")
		if not isinstance(host, str) or not host or host in ("0.0.0.0", "::", "::0", "[::]"):
			host = "127.0.0.1"
		port = pflags.get("port", "8080")
		if isinstance(port, bool) or not port:
			port = "8080"
		url = "http://%s:%s" % ("[%s]" % host if ":" in host and not host.startswith("[") else host, port)
		return url, str(port), (pflags, watched)

	# ---- what a server's say-so may start or open: only the llama-server this machine runs and that serves the watched URL ----
	def watched_exe(self, refresh=False):
		"""The image path of the llama-server process that serves the watched URL, or None (then `trust_reason` says why).

		Helper programs (`llama-server --help`, `llama-fit-params`) run only from this one file's folder, and the model, draft-model and log-file
		paths a server reports are opened only while it is set. A server is trusted when its address is a loopback address and the process
		listening on that port is named llama-server*; nothing the server reports (status.args, ...) is ever used to find a program.
		"""
		url = self.cfg.get("router") or ""
		if refresh or self._trust is None or self._trust[0] != url:
			exe, reason = self._find_trusted(url)
			self._trust, self.trust_reason = (url, exe), reason
		return self._trust[1]

	def _find_trusted(self, url):
		if not win.WIN:
			return None, "this is not Windows"
		u = urllib.parse.urlsplit(url)
		try:
			port = u.port or (443 if u.scheme == "https" else 80)
		except ValueError:
			return None, "the server address has no valid port"
		if not is_loopback_host(u.hostname):
			return None, "the server at %s is not at a loopback address" % url
		pid = win.tcp_listener_pid(port, u.hostname)
		if pid is None:
			return None, "no single process listens on port %d of the loopback address" % port
		if not (self.procs.get(pid) or win.processes().get(pid) or ("",))[0].startswith("llama-server"):
			return None, "the process listening on port %d (pid %d) is not named llama-server*" % (port, pid)
		info = self._proc_info(pid)
		if not info.get("exe"):
			info = self.pinfo[pid] = win.proc_info(pid)  # a failed read is not kept: try again
		exe = info.get("exe")
		if not exe or not os.path.isabs(exe) or not os.path.basename(exe).lower().startswith("llama-server"):
			return None, "the image path of llama-server (pid %d) could not be read" % pid
		return exe, ""

	def _aliases(self):
		exe = self.watched_exe()
		if not exe:
			return {}
		with self._alias_lock:
			if exe in self._alias_cache:
				return self._alias_cache[exe]
			if exe in getattr(model, "_ALIASES", {}):
				self._alias_cache[exe] = model._ALIASES[exe]
				return self._alias_cache[exe]
			if exe not in self._alias_threads:
				def _fetch():
					try:
						res = model.server_aliases(exe)
						with self._alias_lock:
							self._alias_cache[exe] = res or {}
					except Exception:
						pass
					finally:
						with self._alias_lock:
							self._alias_threads.pop(exe, None)
				t = threading.Thread(target=_fetch, daemon=True)
				self._alias_threads[exe] = t
				t.start()
			return {}

	@property
	def key_allowed(self):
		"""The API key goes only to a URL the user gave or one taken from a running llama-server's command line, never to the default-address probe."""
		return self.router_explicit or self.router_keyed

	def _vet_path(self, preset, flag, value):
		"""`value` when the server is trusted and it is an absolute path on a local drive, else None (with a notice for a refused path)."""
		key = (preset, flag)
		if self.watched_exe() is None:
			self.rejected.pop(key, None)  # the notice that there is no trusted server says why
			return None
		if model.local_path(value):
			self.rejected.pop(key, None)
			return value
		self.rejected[key] = "%s: the %s path is ignored: it is not an absolute path on a local drive" % (preset, {"model": "model", "model-draft": "draft model", "log-file": "log file"}.get(flag, flag))
		return None

	def _single_model_pid(self):
		candidates = self._independent_pids() or list(self.pids)
		if not candidates:
			return None
		u = urllib.parse.urlsplit(self.cfg.get("router") or "")
		target_port = str(u.port or (443 if u.scheme == "https" else 80))
		port_match = None
		for pid in candidates:
			url, port, _ = self._pid_url_and_flags(pid)
			if url == self.cfg.get("router"):
				return pid
			if port == target_port and port_match is None:
				port_match = pid
		if port_match is not None:
			return port_match
		if len(candidates) == 1:
			return candidates[0]
		return None

	def _model_pids(self):
		if self.mode == "single":
			mpid = self._single_model_pid()
			return [mpid] if mpid is not None else []
		kids = [p for p in self.pids if self.procs.get(self.procs.get(p, ("", 0))[1], ("",))[0].startswith("llama-server")]
		if not kids and not self.models:  # standalone llama-server before mode is known
			kids = list(self.pids)
		return kids

	def _child_created(self, preset):
		if not self.pids and win.WIN:
			self._update_procs(time.time())
		m = self.models.get(preset)
		if m is not None and isinstance(m.get("status"), dict) and m["status"].get("value") != "loaded":
			return None
		kids = self._model_pids()
		if not kids:
			return None
		if len(kids) == 1:
			return self._proc_info(kids[0]).get("created")
		mpath = self.flags.get(preset, ({}, ""))[0].get("model")
		for pid in kids:
			info = self._proc_info(pid)
			argv = info.get("argv") or ()
			if preset in argv or (mpath and mpath in argv):
				return info.get("created")
		return self._proc_info(kids[0]).get("created")

	def preset_log_path(self, preset):
		flags = self.flags.get(preset, ({}, ""))[0]
		lf = flags.get("log-file")
		if isinstance(lf, str) and lf and self._vet_path(preset, "log-file", lf):
			return lf
		if self.cfg.get("log_dir"):
			safe = RE_BAD_FILE.sub("_", preset)
			return os.path.join(self.cfg["log_dir"], safe + ".log")
		return None

	# ---- events -----------------------------------------------------------------------------------------------------
	def emit(self, level, kind, msg, wall=None, preset=None, detail=None):
		ev = {"wall": wall or time.time(), "t": 0, "level": level, "kind": kind, "msg": msg, "preset": preset, "session": None}
		if detail:
			ev["detail"] = detail
		self.add_events([ev])
		if self.hist and self.cfg["persist"] and kind == "alert":
			self.hist.write_event(ev)

	def add_events(self, evs):
		self.events.extend(evs)
		if len(self.events) > 6000:
			del self.events[:1000]

	# ---- startup: replay history ------------------------------------------------------------------------------------
	def load_history(self):
		"""Persisted samples and events, then every preset log and the router log: so the views start populated."""
		if win.WIN:
			self._update_procs(time.time())
		if self.hist and not self.state_error:
			samples, events = self.hist.load(time.time() - self.cfg["history_hours"] * 3600)
			now = time.time()
			for t, vals in samples:
				for k, x in vals.items():
					if k in self.rings:
						for dt in range(5):  # one persisted sample stands for the 5 s around it
							if t + dt <= now:
								self.rings[k].put(t + dt, x)
			self.add_events(events)
		spawns = logmod.session_starts(self.cfg.get("log_dir"))
		self.poll_models(time.time())
		log_dir = self.cfg.get("log_dir")
		names = list(self.models)
		if not names and log_dir and os.path.isdir(log_dir):
			try:
				names = sorted(f[:-4] for f in os.listdir(log_dir) if f.endswith(".log") and not f.startswith("llama-server"))
			except OSError:
				names = []
		for name in names:
			self.open_preset(name, spawns.get(name, []))
		self.follow_router()
		saved = self.reqlog.load() if self.reqlog and not self.state_error else []  # requests kept from earlier runs, including those whose log has since been rewritten
		known = defaultdict(list)
		for r in saved:
			known[(r["preset"], r["task"])].append(r.get("wall") or 0)
		fresh = [r for r in self.requests if not any(r.get("wall") is None or abs(r["wall"] - w) <= 15 for w in known.get((r["preset"], r["task"]), ()))]
		if self.cfg["persist"]:
			for r in fresh:
				if r.get("wall"):
					self.reqlog.append(r)
		self.requests = saved + fresh
		self.req_seq += 1
		self.requests.sort(key=lambda r: r.get("wall") or 0)
		self.events.sort(key=lambda e: e["wall"])
		for f in self.follow.values():
			f[1].live = True

	def follow_router(self):
		"""Tail the newest router log (a restarted router writes a new file)."""
		if not self.pids and win.WIN:
			self._update_procs(time.time())
		if self.mode == "single":
			if not self.cfg.get("router_log") and not self.cfg.get("log_dir"):
				self.router_tail = None
				return
			router_pid = None
		else:
			router_pid = self._single_model_pid()
		rinfo = self._proc_info(router_pid) if router_pid else {"exe": None, "argv": None, "created": None}
		router_created = rinfo.get("created")
		routers = []
		if self.cfg.get("router_log"):
			routers = logmod.router_logs(None, [self.cfg["router_log"]])
		elif rinfo.get("argv"):
			rflags = model.parse_args(rinfo["argv"], self._aliases())
			rlf = rflags.get("log-file")
			if isinstance(rlf, str) and rlf and self._vet_path("router", "log-file", rlf):
				routers = logmod.router_logs(None, [rlf])
		if not routers and self.cfg.get("log_dir"):
			routers = logmod.router_logs(self.cfg["log_dir"])
		if routers and (self.router_tail is None or routers[-1][1] != self.router_tail.path):
			start, path = routers[-1]
			if start is None:
				start = router_created
			self.router_tail, self.router_start = logmod.Tail(path, backfill=4 * MIB), start
			self.add_events(logmod.parse_router(self.router_tail.poll(), start))

	def open_preset(self, preset, spawns=()):
		path = self.preset_log_path(preset)
		if not path:
			return
		tail, asm = logmod.Tail(path), logmod.Assembler(preset)
		reqs, evs = [], []
		for line in tail.poll():
			d, e = asm.feed(line)
			reqs += d
			evs += e
		sp_list = list(spawns)
		if not sp_list:
			created = self._child_created(preset)
			if created:
				sp_list = [created]
		logmod.resolve_walls(asm, reqs, evs, sp_list)
		self.requests += reqs
		self.req_seq += 1
		self.add_events([e for e in evs if e.get("wall")])
		self.follow[preset] = (tail, asm)

	# ---- HTTP -------------------------------------------------------------------------------------------------------
	def get(self, path, timeout=None):
		"""GET `path` from the watched server as JSON. No redirect is followed, and the API key (an unredirected header) goes only where `key_allowed` says."""
		budget = (0.5 if path.startswith("/slots") else 1.0) if timeout is None else float(timeout)
		t0 = time.monotonic()
		deadline = t0 + budget
		req = urllib.request.Request(self.cfg["router"] + path)
		if self.cfg.get("api_key") and self.key_allowed:
			req.add_unredirected_header("Authorization", "Bearer " + str(self.cfg["api_key"]))
		rem = deadline - time.monotonic()
		if rem <= 0:
			raise TimeoutError("request timed out before connect")
		with opener_for(self.cfg["router"]).open(req, timeout=rem) as r:
			headers = getattr(r, "headers", None)
			if headers and hasattr(headers, "get"):
				try:
					cl = int(headers.get("Content-Length") or 0)
					if cl > 8 * MIB:
						raise OSError("reply body exceeded 8 MiB limit")
				except (ValueError, TypeError):
					pass
			body = bytearray()
			read_fn = getattr(r, "read1", r.read)
			while True:
				rem = deadline - time.monotonic()
				if rem <= 0:
					raise TimeoutError("request timed out")
				sock = getattr(r, "sock", None)
				if sock is None:
					fp = getattr(r, "fp", None)
					raw = getattr(fp, "raw", None) if fp else None
					sock = getattr(raw, "_sock", None) if raw else None
				if sock and hasattr(sock, "settimeout"):
					try:
						sock.settimeout(max(0.001, rem))
					except (OSError, ValueError):
						pass
				chunk = read_fn(65536)
				if not chunk:
					break
				if isinstance(chunk, str):
					chunk = chunk.encode("utf-8")
				body.extend(chunk)
				if len(body) > 8 * MIB:
					raise OSError("reply body exceeded 8 MiB limit")
			return json.loads(body.decode("utf-8"))

	def is_due(self, key, now, period):
		if now >= self.due.get(key, 0):
			self.due[key] = now + period
			return True
		return False

	# ---- the tick ---------------------------------------------------------------------------------------------------
	def tick(self, now=None):
		now = now or time.time()
		if self.cfg.get("api_key") != self._last_api_key:
			self._last_api_key = self.cfg.get("api_key")
			for k in [k for k in self.due if k == "models" or k.startswith("slots:")]:
				self.due.pop(k, None)
		new_events = []
		if self.is_due("procs", now, 1.0) and win.WIN:
			self._update_procs(now)
		if self.is_due("models", now, 1.0):
			self.poll_models(now)
		if self.is_due("router-log", now, 5.0):
			self.follow_router()
		# log tails first, so the live view knows about a new request immediately
		for _preset, (tail, asm) in self.follow.items():
			for line in tail.poll():
				d, e = asm.feed(line)
				for r in d:
					self.finish_request(r, now)
				self.add_events(e)
				new_events += e
		if self.router_tail:
			evs = logmod.parse_router(self.router_tail.poll(), None)
			self.add_events(evs)
			new_events += evs
		snap = self.sample(now, new_events)
		self.last = snap
		return snap

	def _discover_router(self, first_err):
		if not self.pids and win.WIN:
			self._update_procs(time.time())
		kids = {p for p in self.pids if self.procs.get(self.procs.get(p, ("", 0))[1], ("",))[0].startswith("llama-server")}
		ordered = [p for p in self.pids if p not in kids] + [p for p in self.pids if p in kids]
		candidates = []
		seen = set()
		for pid in ordered:
			url, _, _ = self._pid_url_and_flags(pid)
			if url and url not in seen:
				seen.add(url)
				candidates.append((pid, url))
		if not candidates:
			self.router_tried = [self.cfg["router"]]
			self.router_discovery = None
			return None, first_err

		if self._discover_idx == 0 or self._discover_idx >= len(candidates):
			self._discover_idx = 0
			self._discover_tried = []

		if not self._discover_tried:
			tried = [self.cfg["router"]]
		else:
			tried = list(self._discover_tried)

		pid, url = candidates[self._discover_idx]
		self._discover_idx = (self._discover_idx + 1) % len(candidates)
		if url not in tried:
			tried.append(url)
		self._discover_tried = tried

		prev_router = self.cfg["router"]
		prev_keyed = self.router_keyed
		self.cfg["router"] = url
		self.router_keyed = True  # the URL comes from a running llama-server's own command line: the API key may be sent to it

		try:
			data = self.get("/models")["data"]
			self.router_tried = tried
			self.router_discovery = "server found at %s from llama-server (pid %d) command line (tried %s)" % (url, pid, ", ".join(tried))
			self._discover_tried = []
			self._discover_idx = 0
			return data, None
		except urllib.error.HTTPError as e:
			if e.code in (401, 403):
				self.router_tried = tried
				self.router_discovery = "server found at %s from llama-server (pid %d) command line (tried %s)" % (url, pid, ", ".join(tried))
				self._discover_tried = []
				self._discover_idx = 0
				return None, e
			first_err = e
		except (OSError, ValueError, KeyError) as e:
			first_err = e

		self.cfg["router"] = prev_router
		self.router_keyed = prev_keyed
		self.router_tried = tried
		self.router_discovery = None
		return None, first_err

	def poll_models(self, now):
		self.watched_exe(refresh=True)
		try:
			data = self.get("/models")["data"]
			if not self.router_explicit and not self.router_discovery:
				self.router_discovery = "server found at %s (default)" % self.cfg["router"]
		except (OSError, ValueError, KeyError) as e:
			err = e
			if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
				if not self.router_explicit and not self.router_discovery:
					self.router_discovery = "server found at %s (default)" % self.cfg["router"]
				data = None
				if self.cfg.get("api_key") and not self.key_allowed:
					# the default address was asked without the key: if a running llama-server's command line names this address, ask it again with the key
					data, err = self._discover_router(e)
			elif self.router_explicit:
				data = None
			else:
				data, err = self._discover_router(e)
			if data is None:
				if isinstance(err, urllib.error.HTTPError) and err.code in (401, 403):
					self.router_auth_error = "HTTP %d" % err.code
					self.router_up = False
					self.router_error = "%s (%s)" % (self.router_auth_error, "pass --api-key or set LLMTOP_API_KEY / LLAMA_API_KEY" if self.key_allowed else
						"no API key was sent to the default address: pass --router with --api-key or set LLMTOP_API_KEY / LLAMA_API_KEY")
					self.models, self.mode = {}, None
				else:
					self.router_auth_error = None
					self.router_up, self.router_error, self.mode = False, str(err)[:80], None
				return
		self.router_auth_error = None
		self.models = {m["id"]: m for m in data if isinstance(m, dict) and "id" in m}
		self.router_up, self.router_error = True, ""
		self.mode = "single" if (self.models and any(not isinstance(m.get("status"), dict) for m in self.models.values())) else "router"
		for name, m in self.models.items():
			if isinstance(m.get("status"), dict):
				args = m["status"].get("args")
				if isinstance(args, list) and args and all(isinstance(a, str) for a in args):
					# args[0] is only the server's word for its own program: it is never used to find or start anything
					self.flags[name] = (model.parse_args(args, self._aliases()), self.watched_exe())
			else:
				if not self.pids and win.WIN:
					self._update_procs(now)
				mpid = self._single_model_pid()
				fe = self._pid_url_and_flags(mpid)[2] if mpid is not None else None
				if fe is not None:
					self.flags[name] = fe
				else:
					self.flags.pop(name, None)
			path = self.preset_log_path(name)
			if path and (name not in self.follow or self.follow[name][0].path != path) and self.last is not None:
				self.open_preset(name)
				if name in self.follow:
					self.follow[name][1].live = True

	def finish_request(self, r, now):
		r["wall"] = r.get("wall") or now
		r["wall0"] = r.get("wall0") or r["wall"] - r.get("ms", 0) / 1e3
		self.requests.append(r)
		self.req_seq += 1
		if len(self.requests) > 60000:
			del self.requests[:len(self.requests) - 50000]  # the append's req_seq increment covers the drop too
		if self.cfg["persist"]:
			self.reqlog.append(r)

	def _select_gpu(self, pd, kids):
		self.gpu_notice = None
		adapters = getattr(self.dx, "adapters", None) if self.dx else None
		if not adapters:
			return self.gpu
		spec = self.cfg.get("gpu")
		if spec is not None and spec != "":
			if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().isdigit()):
				idx = int(spec)
				if 0 <= idx < len(adapters):
					return adapters[idx]
				self.gpu_notice = "GPU index %s not in 0..%d" % (spec, len(adapters) - 1)
			else:
				sub = str(spec).lower()
				found = next((a for a in adapters if sub in a.get("name", "").lower()), None)
				if found is not None:
					return found
				self.gpu_notice = "no GPU matching %r" % spec
		by_luid = pd.get("procs_by_luid") or {}
		if kids and by_luid:
			scored = [(sum(by_luid.get(p, {}).get(a.get("luid"), {}).get("ded", 0.0) + by_luid.get(p, {}).get(a.get("luid"), {}).get("shr", 0.0) for p in kids), a.get("vram", 0.0), -i)
				for i, a in enumerate(adapters)]
			best_score = max(scored)
			if best_score[0] > 0:
				return adapters[-best_score[2]]
		return self.dx.main() if hasattr(self.dx, "main") else max(adapters, key=lambda a: a.get("vram", 0.0), default=None)

	def _build_notices(self, presets, kids):
		notices = []
		if self.state_error:
			notices.append("state folder %s is unavailable: %s" % (self.state_dir, self.state_error))
		if not self.router_explicit:
			if (self.router_up or self.router_auth_error) and self.router_discovery and not self.router_discovery.endswith("(default)"):  # the default URL working is not news
				notices.append(self.router_discovery)
			elif not self.router_up and not self.router_auth_error and self.router_tried:
				notices.append("server unreachable (tried %s)" % ", ".join(self.router_tried))
		indep = self._independent_pids()
		if len(indep) > 1:
			notices.append("%d llama-server instances found; watching %s (pass --router to choose)" % (len(indep), self.cfg["router"]))
		if self.mode == "single":
			notices.append("single-model mode")
		if self.router_up and self.models and self.watched_exe() is None:
			notices.append(self.TRUST_OFF % self.trust_reason)
		notices.extend(msg for (name, _flag), msg in sorted(self.rejected.items()) if name in self.models or name == "router")
		if self.gpu_notice:
			notices.append(self.gpu_notice)
		loaded = [n for n, p in presets.items() if p["status"] == "loaded"]
		if any(p["status"] in ("loaded", "loading") for p in presets.values()) and not kids:
			notices.append("llama-server: no process named llama-server* found, so its GPU and RAM figures read 0 (a renamed or wrapped server is not recognised)")
		if len(kids) > 1 or len(loaded) > 1:
			notices.append("%d model processes resident: VRAM calibration is paused" % max(len(kids), len(loaded)))
		for name in loaded:
			if name not in self.follow or not os.path.isfile(self.follow[name][0].path):
				notices.append("no log file for preset %s" % name)
		for name, (_, asm) in self.follow.items():
			if asm.lines_seen > 0 and asm.lines_parsed == 0:
				notices.append("%s: lines are being read (%d) but none match the expected format" % (name, asm.lines_seen))
			elif asm.lines_parsed > 0 and not asm.timestamps:
				notices.append("%s: the log has no timestamps (pass --log-timestamps for wall-clock history)" % name)
		return notices

	def sample(self, now, new_events):
		pd = self.counters.sample() if self.counters else {"procs": {}, "procs_by_luid": {}, "adapters": {}, "sys": {}}
		sm = win.sys_mem() if win.WIN else {}
		if sm:
			sm["commit_max"] = max(sm["commit_limit"], sm["total"] + self.pagefile_max) if self.pagefile_max else sm["commit_limit"]
		kids = self._model_pids()
		self.gpu = self._select_gpu(pd, kids)
		luid = self.gpu.get("luid") if self.gpu else None
		ad = pd.get("adapters", {}).get(luid, {"ded": 0.0, "shr": 0.0, "com": 0.0}) if self.gpu else None
		gpu = None
		if self.gpu and ad is not None:
			uma = self.gpu.get("vram", 0.0) <= 0
			budget = self._gpu_budget(self.gpu)
			if budget > 0:
				scale_thresholds(self.cfg, None, None, budget, self.user_thresholds)
			gpu = {"name": self.gpu["name"], "vram": self.gpu["vram"], "shared": self.gpu.get("shared", 0.0), "budget": budget,
				"ded": ad["ded"], "shr": ad["shr"], "com": ad["com"], "uma": uma}
		by_luid = pd.get("procs_by_luid")
		use_luid = bool(luid and isinstance(by_luid, dict) and by_luid)
		proc_gpu = (lambda pid_: by_luid.get(pid_, {}).get(luid)) if use_luid else (lambda pid_: pd.get("procs", {}).get(pid_))
		ll = {"present": bool(kids), "pids": kids, "ded": 0.0, "shr": 0.0, "com": 0.0, "eng": {}, "ws": 0.0, "pws": 0.0, "commit": 0.0, "cpu": 0.0,
			"faults_s": 0.0, "ws_drop": 0.0, "commit_drop": 0.0, "allow": 0.0, "over": 0.0, "age": 0.0, "vm": None, "vm_error": None, "pm_error": None,
			"allow_known": False, "allow_src": "projected", "floor": 0.0, "floor_known": False}
		for pid_ in kids:
			p = proc_gpu(pid_)
			if p:
				for k in ("ded", "shr", "com"):
					ll[k] += p[k]
				for k, v in p["eng"].items():
					ll["eng"][k] = min(100.0, ll["eng"].get(k, 0.0) + v)
			pm = win.proc_mem(pid_)
			if pm:
				for k in ("ws", "pws", "commit"):
					ll[k] += pm[k]
				prev = self.prev.get(("pm", pid_))
				if prev and now > prev[0]:
					ll["cpu"] += max(0.0, (pm["cpu_s"] - prev[1]["cpu_s"]) / (now - prev[0]))
					ll["faults_s"] += max(0.0, (pm["faults"] - prev[1]["faults"]) / (now - prev[0]))
				self.prev[("pm", pid_)] = (now, pm)
			else:
				ll["pm_error"] = _win_err("proc_mem", pid_) or ("proc_mem failed for pid %d" % pid_)
		if win.WIN and self.is_due("hostmem", now, 2.0):
			self.ws_by, self.ws_total = win.working_sets(self.procs)
			self.pagefiles = win.pagefiles()
		if kids and not self.vm_busy and self.is_due("vm", now, 5.0):
			self.vm_busy = True
			target_pid = max(kids, key=lambda p: (proc_gpu(p) or {}).get("ded", 0.0))
			threading.Thread(target=self.vm_job, args=(target_pid,), daemon=True).start()
		ll["vm"] = self.vm if kids else None
		ll["vm_error"] = self.vm_error if kids else None
		if kids:
			ll["age"] = now - self.pid_seen.setdefault(tuple(kids), now)
		self.ws_hist.append((now, ll["ws"], ll["commit"]))
		recent = [h for h in self.ws_hist if now - h[0] <= 10]
		ll["ws_drop"] = max(0.0, max((h[1] for h in recent), default=0.0) - ll["ws"]) if ll["present"] else 0.0
		ll["commit_drop"] = max(0.0, max((h[2] for h in recent), default=0.0) - ll["commit"]) if ll["present"] else 0.0
		groups = {}
		proc_keys = by_luid.keys() if use_luid else pd.get("procs", {}).keys()
		for pid_ in proc_keys:
			if pid_ in self.pids:
				continue
			p = proc_gpu(pid_)
			if not p:
				continue
			exe = self.procs.get(pid_, ("pid %d" % pid_,))[0]
			g = groups.setdefault(exe, {"exe": exe, "ded": 0.0, "shr": 0.0, "eng": 0.0, "n": 0})
			g["ded"], g["shr"], g["n"] = g["ded"] + p["ded"], g["shr"] + p["shr"], g["n"] + 1
			g["eng"] = max(g["eng"], max(p["eng"].values(), default=0.0))
		others = sorted((g for g in groups.values() if g["ded"] >= 64 or g["eng"] >= 5), key=lambda g: -g["ded"])
		sy = pd.get("sys", {})
		ram = None
		if sm:
			ram = {**sm, "pages_in": sy.get("pages_in", 0.0), "pages_out": sy.get("pages_out", 0.0), "standby": sy.get("standby", 0.0),
				"pagefile_pct": sy.get("pagefile_pct", 0.0), "free": sy.get("free", 0.0), "modified": sy.get("modified", 0.0),
				"pagefiles": self.pagefiles, "pf_cfg": self.pf_cfg, "ws_by": self.ws_by, "ws_total": self.ws_total}
		cpu = {"cpu": sy.get("cpu", NAN), "cpu_mhz": sy.get("cpu_mhz", NAN), "cpu_queue": sy.get("cpu_queue", NAN), "cpu_cores": sy.get("cpu_cores", [])}
		presets = {}
		for pid_, m in self.models.items():
			has_status = isinstance(m.get("status"), dict)
			st = m["status"].get("value", "unloaded") if has_status else "loaded"
			presets[pid_] = {"status": st, "meta": m.get("meta") or {}, "slot": None, "slot_error": None, "phase": "idle", "kv": 0, "rate_dec": NAN, "rate_pre": NAN,
				"eta": NAN, "elapsed": NAN, "n_slots": 0, "n_busy": 0, "exit": m["status"].get("exit_code") if has_status else None}
			if st == "loaded":
				self.sample_slot(pid_, presets[pid_], now)
		busy = any(p["phase"] != "idle" for p in presets.values())
		for name, p in presets.items():  # baseline for shared GPU memory: measured on an earlier load if known, else llama.cpp's host-buffer projection
			e = self.mem.get(name)
			if p["status"] == "loaded" and e and e.get("state") == "ready":
				m = e["map"]
				cal = self.calib.get(self.calib_key(e["key"]))
				if cal and cal.get("idle_shared") is not None:
					ll["allow"], ll["allow_src"] = float(cal["idle_shared"]), "measured"
				else:
					ll["allow"], ll["allow_src"] = (m["fit"] or {}).get("host", {}).get("self", m["weights"]["cpu"]), "projected"
				ll["allow_known"] = True
				if kids and ll["ded"] >= 0.9 * sum(r[1] for r in model.memory_rows(m)):  # once the model is resident: the lowest shared figure this process has held
					key = tuple(kids)
					self.shr_floor[key] = min(self.shr_floor.get(key, ll["shr"]), ll["shr"])
					ll["floor"], ll["floor_known"] = self.shr_floor[key], True
		ll["over"] = max(0.0, ll["shr"] - ll["allow"])
		self.allow = ll["allow"] or self.allow
		self.allow_known = self.allow_known or ll["allow_known"]
		notices = self._build_notices(presets, kids)
		snap = {"t": now, "mode": self.mode, "router_up": self.router_up, "router_error": self.router_error, "router_auth_error": self.router_auth_error, "router_key_allowed": self.key_allowed, "gpu": gpu, "llama": ll,
			"vm_error": self.vm_error, "others": others, "ram": ram, "cpu": cpu, "presets": presets, "busy": busy, "new_events": new_events, "notices": notices}
		self.rec.add(now, self.series_values(snap))
		self.alerts.evaluate(snap, self.mem)
		for preset, p in presets.items():
			if p["status"] == "loaded" and preset in self.flags:
				self.ensure_map(preset)
		self.calibrate(snap)
		return snap

	def vm_job(self, pid):
		try:
			vm = win.vm_summary(pid)
			if vm:
				self.vm = dict(vm, pid=pid, at=time.time())
				self.vm_error = None
			else:
				self.vm_error = _win_err("vm_summary", pid) or ("vm_summary failed for pid %d" % pid)
		except Exception as e:
			self.vm_error = _win_err("vm_summary", pid) or ("%s: %s" % (type(e).__name__, e))
		finally:
			self.vm_busy = False

	def sample_slot(self, preset, p, now):
		asm = self.follow.get(preset, (None, None))[1]
		in_log = bool(asm and any(now - (c.get("wall0") or now) < 1800 for c in asm.cur.values()))
		active = self.rate.get(preset, {}).get("active", False) or in_log
		if not self.is_due("slots:" + preset, now, 1.0 / self.cfg["hz"] if active else 3.0):
			p.update(self.rate.get(preset, {}).get("p", {}))
			return
		path = "/slots" if self.mode == "single" else "/slots?model=" + urllib.parse.quote(preset, safe="")
		try:
			slots = self.get(path)
		except urllib.error.HTTPError as e:
			err = "HTTP %d" % e.code
			preset_r = self.rate.setdefault(preset, {})
			prev_p = dict(preset_r.get("p") or {})
			prev_p["slot_error"] = err
			preset_r["p"] = prev_p
			p.update(prev_p)
			return
		except (OSError, ValueError):
			p.update(self.rate.get(preset, {}).get("p", {}))
			return
		if not isinstance(slots, list) or (slots and not all(isinstance(s, dict) for s in slots)):
			err = "unexpected /slots reply"
			preset_r = self.rate.setdefault(preset, {})
			prev_p = dict(preset_r.get("p") or {})
			prev_p["slot_error"] = err
			preset_r["p"] = prev_p
			p.update(prev_p)
			return
		if not slots:
			p["slot_error"] = None
			p["n_slots"] = 0
			p["n_busy"] = 0
			self.rate.setdefault(preset, {})["p"] = dict(p)
			return
		slot_records = []
		for idx, s in enumerate(slots):
			s_id = s.get("id", idx)
			raw_nt = s.get("next_token")
			if isinstance(raw_nt, list) and raw_nt:
				nt = raw_nt[0] if isinstance(raw_nt[0], dict) else {}
			elif isinstance(raw_nt, dict):
				nt = raw_nt
			else:
				nt = {}
			decoded = nt.get("n_decoded", 0)
			proc = bool(s.get("is_processing"))
			n_prompt = s.get("n_prompt_tokens", 0)
			cached = s.get("n_prompt_tokens_cache", 0)
			done = s.get("n_prompt_tokens_processed", 0)
			todo = max(0, n_prompt - cached)
			task = s.get("id_task")
			r = self.rate.setdefault((preset, s_id), {"hist": deque(maxlen=64), "task": None, "t0": now, "dec": NAN, "pre": NAN})
			if task != r["task"]:
				r.update(task=task, t0=now, dec=NAN, pre=NAN)
				r["hist"].clear()
			r["hist"].append((now, done, decoded))
			pts = r.setdefault("pre_pts", deque(maxlen=8))
			if task != r.get("pre_task"):
				pts.clear()
				r["pre_task"] = task
			if proc and (not pts or done != pts[-1][1]):
				pts.append((now, done))
			if proc:
				cur = (asm.cur.get(task) if asm else None) or {}
				if cur.get("wall0") and cur["wall0"] > now - 3600:
					r["t0"] = min(r["t0"], cur["wall0"])
				dec = self.windowed(r["hist"], 2, 2.0, now)
				pre = self.prefill_rate(pts, now)
				if decoded > 0 and dec == dec:
					r["dec"] = dec
				if decoded == 0 and pre == pre:
					r["pre"] = pre
			phase = "idle" if not proc else "prefill" if decoded == 0 and done < todo else "starting" if decoded == 0 else "decode"
			kv = cached + done + decoded if proc else (cached + done + decoded if (cached or done) else (n_prompt + decoded))
			eta = (todo - done) / r["pre"] if phase == "prefill" and r["pre"] == r["pre"] and r["pre"] > 0 else NAN
			elapsed = now - r["t0"] if proc else NAN
			r["active"] = proc
			slot_records.append({
				"s": s, "s_id": s_id, "proc": proc, "task": task, "t0": r["t0"],
				"n_prompt": n_prompt, "cached": cached, "done": done, "todo": todo,
				"decoded": decoded, "remain": nt.get("n_remain", 0), "nt": nt,
				"phase": phase, "kv": kv, "rate_dec": r["dec"] if proc else NAN,
				"rate_pre": r["pre"] if proc else NAN, "eta": eta, "elapsed": elapsed,
			})
		proc_slots = [sr for sr in slot_records if sr["proc"]]
		if proc_slots:
			primary = max(proc_slots, key=lambda sr: (sr["t0"], sr["task"] if isinstance(sr["task"], (int, float)) else -1))
		else:
			finished = [sr for sr in slot_records if isinstance(sr["task"], (int, float)) and sr["task"] is not None]
			if finished:
				primary = max(finished, key=lambda sr: sr["task"])
			else:
				primary = next((sr for sr in slot_records if sr["s_id"] == 0), slot_records[0])
		ps = primary["s"]
		p_proc = primary["proc"]
		p_kv = primary["cached"] + primary["done"] + primary["decoded"] if p_proc else (self.last_kv(preset) or primary["n_prompt"] + primary["decoded"])
		all_same_n_ctx = len(set(sr["s"].get("n_ctx", 0) for sr in slot_records)) == 1
		all_toks = [p_kv if sr is primary else sr["kv"] for sr in slot_records]
		slots_with_tokens = sum(1 for t in all_toks if t > 0)
		all_tokens = sum(all_toks) if (all_same_n_ctx and slots_with_tokens > 1) else None
		out = {
			"slot": {
				"id": primary["s_id"], "n_ctx": ps.get("n_ctx", 0), "task": primary["task"],
				"n_prompt": primary["n_prompt"], "cached": primary["cached"], "done": primary["done"],
				"todo": primary["todo"], "decoded": primary["decoded"], "remain": primary["remain"],
				"params": ps.get("params") or {}, "speculative": ps.get("speculative", False),
			},
			"slot_error": None, "phase": primary["phase"], "kv": p_kv,
			"rate_dec": primary["rate_dec"], "rate_pre": primary["rate_pre"], "eta": primary["eta"],
			"elapsed": primary["elapsed"], "n_slots": len(slots), "n_busy": len(proc_slots),
			"all_tokens": all_tokens,
		}
		preset_r = self.rate.setdefault(preset, {})
		preset_r["active"] = bool(proc_slots)
		preset_r["p"] = out
		p.update(out)

	@staticmethod
	def prefill_rate(pts, now):
		"""Prompt tokens per second between the last two batch completions (progress only advances once per batch); decays if the next batch is overdue."""
		if len(pts) < 2:
			return NAN
		(t0, d0), (t1, d1) = pts[-2], pts[-1]
		return (d1 - d0) / (t1 - t0) if now - t1 <= t1 - t0 else (d1 - d0) / max(1e-6, now - t0)

	@staticmethod
	def windowed(hist, idx, window, now):
		"""Rate of column `idx` of the history over the last `window` seconds (NaN until two points span at least 0.3 s)."""
		pts = [h for h in hist if now - h[0] <= window]
		if len(pts) < 2 or pts[-1][0] - pts[0][0] < 0.3:
			return NAN
		return max(0.0, (pts[-1][idx] - pts[0][idx]) / (pts[-1][0] - pts[0][0]))

	def last_kv(self, preset):
		for r in reversed(self.requests):
			if r["preset"] == preset:
				return r.get("kv")
		return None

	def series_values(self, snap):
		g, ll, rm, cp = snap["gpu"], snap["llama"], snap["ram"], snap.get("cpu")
		v = {"busy": 1.0 if snap["busy"] else 0.0}
		if g:
			v.update(vram=g["ded"], budget=g["budget"], gpu_other=max((o["eng"] for o in snap["others"]), default=0.0))
		if ll["present"]:
			v.update(vram_llama=ll["ded"], spill=ll["shr"], gpu_llama=max(ll["eng"].values(), default=0.0), gpu_copy=ll["eng"].get("Copy", 0.0),
				llama_ws=ll["ws"], llama_cpu=ll["cpu"])
		if cp:
			v.update(cpu=cp["cpu"], cpu_mhz=cp["cpu_mhz"])
		if rm:
			v.update(ram_used=rm["total"] - rm["avail"], ram_avail=rm["avail"], commit=rm["commit"], pages_in=rm["pages_in"], pages_out=rm["pages_out"], standby=rm["standby"], free=rm["free"])
			if rm["pagefiles"]:
				v["pf_used"] = sum(p["used"] for p in rm["pagefiles"])
		if ll["vm"]:
			v["heap"] = ll["vm"]["private"]
		for p in snap["presets"].values():
			if p["status"] == "loaded":
				v["ctx"] = p["kv"]
				if p["rate_dec"] == p["rate_dec"] and p["phase"] == "decode":
					v["dec"] = p["rate_dec"]
				if p["rate_pre"] == p["rate_pre"] and p["phase"] == "prefill":
					v["pre"] = p["rate_pre"]
		return v

	# ---- learned overhead: what the OS measures beyond the projection, per model configuration -----------------------------
	@staticmethod
	def calib_key(key):
		return hashlib.sha1(key.encode()).hexdigest()[:12]

	def calibrate(self, snap):
		"""Per model configuration: resident VRAM minus the projection (overhead), and the lowest shared GPU memory seen while VRAM was not under pressure."""
		ll, g = snap["llama"], snap["gpu"]
		if not ll["present"]:
			return
		loaded = [n for n, p in snap["presets"].items() if p["status"] == "loaded"]
		if len(ll["pids"]) > 1 or len(loaded) > 1:
			return
		for preset, p in snap["presets"].items():
			e = self.mem.get(preset)
			if p["status"] != "loaded" or not e or e.get("state") != "ready":
				continue
			rows_total = sum(r[1] for r in model.memory_rows(e["map"]))
			if ll["ded"] < 0.9 * rows_total:  # the model is not (yet) resident: nothing measured now says anything about its footprint
				continue
			overhead = ll["ded"] - rows_total
			k, pids = self.calib_key(e["key"]), tuple(ll["pids"])
			cur = self.calib.get(k)
			ent, changed = dict(cur) if cur else {"v": 4}, False
			if cur is None or self.calib_pid.get(k) != pids or overhead > cur["overhead"] + 16:
				ent.update(overhead=round(overhead), at=snap["t"], preset=preset, spilled=ll["over"] >= self.cfg["spill_mib"])
				self.calib_pid[k], changed = pids, True
			if g and ll["age"] >= self.cfg["load_grace_s"] / 3 and ll["ded"] + ll["shr"] <= g["budget"] - self.cfg["idle_headroom_mib"]:  # resident plus shared fits with room to spare: nothing could have been demoted
				idle = round(ll["shr"])
				if ent.get("idle_shared") is None or idle < ent["idle_shared"] - 16:
					ent["idle_shared"], changed = idle, True
			if changed:
				self.calib[k] = ent
				if self.cfg["calibrate"] and self.calib_file:
					try:
						os.makedirs(os.path.dirname(self.calib_file), exist_ok=True)
						write_json_atomic(self.calib_file, self.calib, indent=1)
					except (OSError, ValueError) as err:
						self._fail_state_dir(err)

	# ---- memory maps (computed off-thread: llama-fit-params takes about a second) ------------------------------------------
	NO_FLAGS_ERROR = "the server did not report this model's command line (/models has no status.args) and no llama-server process was found for it"
	TRUST_OFF = "no helper program is run and no file path from the server is used: %s"
	NOT_TRUSTED_ERROR = "the model file and the helper programs are not used because %s"

	def ensure_map(self, preset):
		fe = self.flags.get(preset)
		if fe is None:
			# No known flags: the server did not report the model's command line and no matching
			# llama-server process was found, so there is nothing to analyse. Record it as a state
			# the Memory and Tensors pages already render ("could not analyse: …"), not an exception.
			cur = self.mem.get(preset, {})
			if cur.get("key") is None and cur.get("state") == "error":
				return  # already recorded; do not rewrite it every frame
			self.mem[preset] = {"key": None, "state": "error", "error": self.NO_FLAGS_ERROR}
			return
		flags = self._vet_flags(preset, fe[0])
		error = None
		if self.watched_exe() is None:
			error = self.NOT_TRUSTED_ERROR % self.trust_reason
		elif flags is None:
			error = "the model path is not an absolute path on a local drive"
		if error:
			cur = self.mem.get(preset, {})
			if cur.get("key") is None and cur.get("state") == "error" and cur.get("error") == error:
				return  # already recorded; do not rewrite it every frame
			self.mem[preset] = {"key": None, "state": "error", "error": error}
			return
		key_flags = {k: flags.get(k) for k in model.FIT_FLAGS + ("model-draft", "cache-ram", "swa-checkpoints", "ctx-checkpoints")}
		if model.setting(flags, {}, "parallel")[0] in (None, -1, "-1", "auto"):
			n_slots = self.rate.get(preset, {}).get("p", {}).get("n_slots")
			if n_slots:
				key_flags["n_slots"] = n_slots
		key = json.dumps(key_flags, sort_keys=True)
		if self.mem.get(preset, {}).get("key") == key:
			return
		self.mem[preset] = {"key": key, "state": "pending"}
		threading.Thread(target=self.map_job, args=(preset, flags, key), daemon=True).start()

	def _vet_flags(self, preset, flags):
		"""The flags to analyse: a copy without a draft model whose path may not be opened, or None when the model path may not be."""
		use = dict(flags)
		if "model" in use and not self._vet_path(preset, "model", use["model"]):
			return None
		dp = model.draft_path(use)
		if dp and not self._vet_path(preset, "model-draft", dp):
			use.pop("model-draft", None)
			use.pop("spec-draft-model", None)
		return use

	def _fit_key(self, exe, flags):
		"""The configuration a cached llama.cpp memory projection is good for: its flags, the model file, and the llama-fit-params binary. None when any of them cannot be stat'd (no caching for that call)."""
		try:
			ms = os.stat(flags["model"])
			fit_exe = os.path.join(os.path.dirname(exe), "llama-fit-params.exe")
			fit = None
			if os.path.isfile(fit_exe):
				fs = os.stat(fit_exe)
				fit = {"path": fit_exe, "size": fs.st_size, "mtime": int(fs.st_mtime)}
			return json.dumps({
				"flags": {k: flags.get(k) for k in model.FIT_FLAGS},
				"model_size": ms.st_size,
				"model_mtime": int(ms.st_mtime),
				"fit": fit,
			}, sort_keys=True)
		except (OSError, KeyError):
			return None

	def map_job(self, preset, flags, key):
		try:
			exe = self.watched_exe()  # the only program ever started, and only from its own folder
			if exe is None:
				raise PermissionError(self.NOT_TRUSTED_ERROR % self.trust_reason)
			with self._alias_lock:
				t = self._alias_threads.get(exe)
			if t and t.is_alive() and t != threading.current_thread():
				t.join()
			fit_key = self._fit_key(exe, flags)
			if fit_key in self.fit_cache:
				fit = self.fit_cache[fit_key]  # a cached projection
			else:
				fit = model.fit_projection(exe, flags)
				if fit_key is not None and fit is not None:  # a failed run (no projection, a timeout) is retried on the next load, not cached
					with self._fit_lock:
						self.fit_cache[fit_key] = fit
						while len(self.fit_cache) > 64:
							self.fit_cache.pop(next(iter(self.fit_cache)))  # drop the oldest-inserted
						if self.cfg["calibrate"] and self.fit_cache_file:
							try:
								write_json_atomic(self.fit_cache_file, self.fit_cache, indent=1)
							except (OSError, ValueError):
								pass
			n_slots = self.rate.get(preset, {}).get("p", {}).get("n_slots")
			map_res = model.analyze(flags, fit, model.server_defaults(exe), n_slots=n_slots)
			self.mem[preset] = {"key": key, "state": "ready", "map": map_res, "at": time.time()}
		except Exception as e:
			self.mem[preset] = {"key": key, "state": "error", "error": "%s: %s" % (type(e).__name__, e)}

	def close(self):
		if self.rec.sec is not None:
			self.rec.flush()
		if self.hist:
			self.hist.close()


# ---- shared formatting -----------------------------------------------------------------------------------------------

def mib(x):
	return "%.1f GiB" % (x / 1024) if abs(x) >= 10240 else "{:,.0f} MiB".format(x)


def headline(text, limit=72):
	"""One line of at most `limit` characters; longer text is cut at a word boundary and marked with …"""
	text = " ".join(str(text).split())
	if len(text) <= limit:
		return text
	cut = text[:limit - 1]
	sp = cut.rfind(" ")
	if sp > 0:
		cut = cut[:sp]
	return cut + "…"


def basename(path):
	"""The last component of a Windows or POSIX path."""
	return str(path).replace("\\", "/").rsplit("/", 1)[-1]


def hms(seconds):
	seconds = int(max(0, seconds))
	return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60) if seconds >= 3600 else "%d:%02d" % (seconds // 60, seconds % 60)


def median(xs):
	xs = [x for x in xs if x == x]
	return statistics.median(xs) if xs else NAN


def isnan(x):
	return isinstance(x, float) and math.isnan(x)
