# SPDX-License-Identifier: GPL-3.0-or-later
"""llama-server log parsing for llmtop: finished requests with their timings, model sessions, router lifecycle events.

Child logs (one per model process) carry per-request timings; the router log (llama-server-<yyyymmdd-hhmmss>.log) carries model loads,
queueing and evictions. Timestamps in both are relative to process start (M.SS.mmm.uuu) when enabled with --log-timestamps (off by
default), so wall-clock times are recovered from the router's file name plus its "spawning server instance" lines.
"""

import glob
import os
import re
import time

MIB = 2**20
RE_ANSI = re.compile(r"\x1b\[[0-9;]*m")
RE_FWD = re.compile(r"^\[\s*(\d+)\]\s*")  # the router forwards each child's stderr as "[<port>] <line>"
RE_LINE = re.compile(r"^(\d+)\.(\d\d)\.(\d{3})\.(\d{3}) (\w) (.*)$")
RE_LEVEL = re.compile(r"^(\w) (.*)$")  # a line without its (default-off) timestamp: level letter, then text
RE_TASK = re.compile(r"\| task (-?\d+) \|\s*(.*)$")
RE_LCP = re.compile(r"selected slot by LCP similarity, (?:f_)?sim_best = ([\d.]+)")
RE_LRU = re.compile(r"selected slot by LRU, n_tokens = (\d+)")
RE_RATE = r"([\d.]+|-nan\(ind\)|-nan|nan|inf)"  # tokens per second; 0 new tokens prints inf or nan
RE_PEVAL = re.compile(r"^prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens.*?" + RE_RATE + r" tokens per second")
RE_EVAL = re.compile(r"^eval time =\s*([\d.]+) ms /\s*(\d+) tokens.*?" + RE_RATE + r" tokens per second")
RE_TOTAL = re.compile(r"^total time =\s*([\d.]+) ms /\s*(\d+) tokens")
RE_REALLOC = re.compile(r"graph reallocs =\s*(\d+)\s*/\s*(\d+)")
RE_DRAFT = re.compile(r"draft acceptance(?: rate)? = ([\d.]+) \(\s*(\d+) accepted /\s*(\d+) generated\)(?:, mean len =\s*([\d.]+))?")
RE_RELEASE = re.compile(r"stop processing: n_tokens = (\d+)(?:, truncated = (\d+))?")
RE_ROUTER_FILE = re.compile(r"^llama-server-(\d{8})-(\d{6})\.log$")
RE_SPAWN = re.compile(rb"^(\d+)\.(\d\d)\.(\d{3})\.(\d{3}) I srv\s+load: spawning server instance with name=(\S+)", re.M)
# warnings that llama.cpp prints on every load; listed as info rather than warnings
LRU_EVENT_TOKENS = 1000  # an LRU slot pick that replaced at least this many held tokens is listed as an event
NOTICE = re.compile(r"security: |control-looking token|removing it from EOG|override 'tokenizer|sharing with layer|chat template (does NOT )?support")


def rel_seconds(m):
	return int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 1e3 + int(m.group(4)) / 1e6


def rate_of(s):
	"""A tokens-per-second figure; the non-finite spellings (0 new tokens: inf, nan, -nan, -nan(ind)) are recorded as NaN."""
	return float("nan") if s in ("inf", "nan", "-nan", "-nan(ind)") else float(s)


def normalize_line(line):
	"""Strip ANSI colour escapes, an optional router-forwarded "[<port>] " prefix and an optional "M.SS.mmm.uuu L " timestamp
	(timestamps are off by default in llama.cpp). Returns (t or None, level or None, text, port or None)."""
	line = RE_ANSI.sub("", line)
	port = None
	if m := RE_FWD.match(line):
		port = int(m.group(1))
		line = line[m.end():]
	if m := RE_LINE.match(line):
		return rel_seconds(m), m.group(5), m.group(6), port
	if m := RE_LEVEL.match(line):
		return None, m.group(1), m.group(2), port
	return None, None, line, port


class Tail:
	"""Incremental line reader for a growing log file. The first poll starts at most `backfill` bytes from the end."""

	def __init__(self, path, backfill=8 * MIB):
		self.path, self.backfill, self.pos, self.rest = path, backfill, None, b""

	def poll(self):
		try:
			size = os.path.getsize(self.path)
		except OSError:
			return []
		if self.pos is None:
			self.pos = max(0, size - self.backfill)
			skip = self.pos > 0
		else:
			skip = False
			if size < self.pos:
				self.pos, self.rest = 0, b""
		if size == self.pos:
			return []
		try:
			with open(self.path, "rb") as f:
				f.seek(self.pos)
				data = f.read(size - self.pos)
		except OSError:
			return []
		self.pos += len(data)
		data = self.rest + data
		lines = data.split(b"\n")
		self.rest = lines.pop()
		if skip and lines:
			lines.pop(0)
		return [ln.decode("utf-8", "replace").rstrip("\r") for ln in lines]


class Assembler:
	"""Child-log lines in; finished requests, sessions and notable events out. Works on a backlog and on a live tail alike."""

	def __init__(self, preset):
		self.preset = preset
		self.sessions = []  # [{'n', 'load_s', 'wall'}]
		self.cur = {}  # task -> request being assembled
		self.sel = None
		self.live = False  # set after the backlog has been read: lines are stamped with the current time
		self.last_t = 0.0
		self.timestamps = False  # True once any timestamped line is seen; stock llama.cpp logs have none
		self.last_port = None  # port of the last router-forwarded line, so it can be attributed to a child
		self.lines_seen = 0
		self.lines_parsed = 0  # matched the line normaliser: a level letter, with or without a timestamp
		self.tasks_seen = 0  # "processing task" lines
		self.requests_done = 0  # "stop processing" lines that closed a request

	def session(self):
		return self.sessions[-1] if self.sessions else None

	def feed(self, line):
		"""Returns (finished requests, events) produced by this line."""
		self.lines_seen += 1
		t, lvl, text, port = normalize_line(line)
		if lvl is None:
			return [], []
		self.lines_parsed += 1
		if port is not None:
			self.last_port = port
		if t is not None:
			self.timestamps = True
			self.last_t = t
		else:
			t = self.last_t  # no timestamps: the last known one (0.0 before any) keeps the ordering
		wall = time.time() if self.live else None
		events, done = [], []
		if "llama_server: initializing" in text:
			self.sessions.append({"n": len(self.sessions), "load_s": None, "wall": wall})
			self.cur.clear()
			self.sel = None
			return done, events
		if "llama_server: model loaded" in text and self.sessions:
			self.sessions[-1]["load_s"] = t
			events.append(self._event(t, wall, "info", "loaded", "%s ready in %.1f s" % (self.preset, t)))
			return done, events
		if lvl in "WE":
			events.append(self._event(t, wall, "crit" if lvl == "E" else "info" if NOTICE.search(text) else "warn", "log", text[:200]))
			return done, events
		if m := RE_LCP.search(text):
			self.sel = {"sel": "LCP", "sim": float(m.group(1))}
			return done, events
		if m := RE_LRU.search(text):
			self.sel = {"sel": "LRU", "sim": 0.0, "evicted": int(m.group(1))}
			if int(m.group(1)) >= LRU_EVENT_TOKENS:
				events.append(self._event(t, wall, "info", "slot", "slot chosen by LRU (no prefix match above the similarity threshold); it held %s tokens" % format(int(m.group(1)), ",")))
			return done, events
		tm = RE_TASK.search(text)
		if not tm or int(tm.group(1)) < 0:
			return done, events
		task, body = int(tm.group(1)), tm.group(2).strip()
		if body.startswith("processing task"):
			self.tasks_seen += 1
			self.cur[task] = {"preset": self.preset, "session": len(self.sessions) - 1, "task": task, "t0": t, "wall0": wall, **(self.sel or {"sel": "-", "sim": 0.0})}
			self.sel = None
			return done, events
		r = self.cur.get(task)
		if r is None:
			return done, events
		if m := RE_PEVAL.search(body):
			r.update(pms=float(m.group(1)), pn=int(m.group(2)), pp=rate_of(m.group(3)))
		elif m := RE_EVAL.search(body):
			r.update(tms=float(m.group(1)), tn=int(m.group(2)), tg=rate_of(m.group(3)))
		elif m := RE_TOTAL.search(body):
			r["ms"] = float(m.group(1))
		elif m := RE_REALLOC.search(body):
			r["reallocs"], r["reallocs_same"] = int(m.group(2)), int(m.group(1))  # the log labels the two figures "(same size) / (changed size)"
			if int(m.group(2)):
				events.append(self._event(t, wall, "info", "alloc", "task %d: compute graph reallocated with changed buffer sizes %d time(s) (log: %d same-size, %d changed-size)" % (task, int(m.group(2)), int(m.group(1)), int(m.group(2)))))
		elif m := RE_DRAFT.search(body):
			r.update(acc=float(m.group(1)), mlen=float(m.group(4)) if m.group(4) is not None else None)
		elif m := RE_RELEASE.search(body):
			self.cur.pop(task)
			self.requests_done += 1
			r.update(t=t, wall=wall, kv=int(m.group(1)), trunc=bool(int(m.group(2))) if m.group(2) is not None else False)
			r["cached"] = max(0, r["kv"] - r["tn"] - r["pn"]) if "pn" in r and "tn" in r else None  # derived: tokens in the slot at release, minus generated, minus newly evaluated
			if r["trunc"]:
				events.append(self._event(t, wall, "warn", "trunc", "task %d: the log reports truncated = 1" % task))
			done.append(r)
		return done, events

	def _event(self, t, wall, level, kind, msg):
		if kind in ("log", "slot", "alloc", "trunc"):
			msg = "%s: %s" % (self.preset, msg)
		return {"t": t, "wall": wall, "level": level, "kind": kind, "msg": msg, "preset": self.preset, "session": len(self.sessions) - 1}


def parse_router(lines, router_start=None):
	"""Router log lines -> events (lifecycle, queueing, errors). Wall time comes from the file's start when known.
	Lines without a timestamp (the default) are ordered by the last known one, 0.0 before any."""
	events = []
	last_t = 0.0
	for line in lines:
		t, lvl, text, port = normalize_line(line)
		if lvl is None:
			continue
		if t is not None:
			last_t = t
		else:
			t = last_t
		wall = router_start + t if router_start else time.time()
		ev = None
		if mm := re.search(r"join: request for name=(\S+) queued at position (\d+)", text):
			ev = ("info", "queue", "request for %s queued (position %s)" % mm.groups())
		elif mm := re.search(r"ensure_model: slot available, loading queued model name=(\S+)", text):
			ev = ("info", "load", "loading %s" % mm.group(1))
		elif mm := re.search(r"tick: evicting idle LRU name=(\S+)", text):
			ev = ("info", "unload", "router evicts idle model %s for a queued request" % mm.group(1))
		elif mm := re.search(r"run: instance name=(\S+) exited with status (-?\d+)", text):
			ev = ("info" if mm.group(2) == "0" else "crit", "exit", "%s exited with status %s" % mm.groups())
		elif mm := re.search(r"got exception: .*?\"message\":\"(.*?)\"", text):
			ev = ("warn", "error", "request failed: " + mm.group(1)[:160])
		elif lvl in "WE" and "got exception" not in text:
			ev = ("crit" if lvl == "E" else "info" if NOTICE.search(text) else "warn", "router", text[:200])
		if ev:
			events.append({"t": t, "wall": wall, "level": ev[0], "kind": ev[1], "msg": ev[2], "preset": None, "session": None, "port": port})
	return events


def router_log_start(path):
	"""The epoch in a router log's file name (local time), or None when the name carries no start time."""
	m = RE_ROUTER_FILE.match(os.path.basename(path))
	if m:
		return time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))
	return None


def router_logs(log_dir, paths=()):
	"""[(start epoch, path)] of router logs, oldest first: the llama-server-<date>-<time>.log files of log_dir
	(None: no folder to scan) plus any explicit paths, whose start time comes from the name when it has one."""
	out = []
	if log_dir:
		for path in glob.glob(os.path.join(log_dir, "llama-server-*.log")):
			start = router_log_start(path)
			if start is not None:
				out.append((start, path))
	for path in paths:
		out.append((router_log_start(path), path))
	return sorted(out, key=lambda p: (p[0] is None, p[0] or 0, p[1]))


def session_starts(log_dir):
	"""{preset: [epoch of each model spawn]} across all router logs, oldest first. log_dir may be None (no folder to scan)."""
	starts = {}
	for start, path in router_logs(log_dir):
		try:
			with open(path, "rb") as f:
				data = f.read()
		except OSError:
			continue
		for m in RE_SPAWN.finditer(data):
			t = int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 1e3 + int(m.group(4)) / 1e6
			starts.setdefault(m.group(5).decode(), []).append(start + t)
	return starts


def resolve_walls(asm, requests, events, spawns):
	"""Give backlog requests and events wall-clock times: session k of the child log is matched to the k-th latest spawn of that preset."""
	n, m = len(asm.sessions), len(spawns)
	for s in asm.sessions:
		j = m - (n - s["n"])
		if s["wall"] is None and 0 <= j < m:
			s["wall"] = spawns[j] + 0.15
	for item in list(asm.cur.values()):
		s = asm.sessions[item["session"]] if 0 <= item["session"] < n else None
		if item.get("wall0") is None and s and s["wall"] is not None:
			item["wall0"] = s["wall"] + item["t0"]
	for item in requests:
		s = asm.sessions[item["session"]] if 0 <= item["session"] < n else None
		if item.get("wall") is None and s and s["wall"] is not None:
			item["wall"] = s["wall"] + item["t"]
			item["wall0"] = s["wall"] + item["t0"]
	for ev in events:
		s = asm.sessions[ev["session"]] if ev.get("session") is not None and 0 <= ev["session"] < n else None
		if ev.get("wall") is None and s and s["wall"] is not None:
			ev["wall"] = s["wall"] + ev["t"]
