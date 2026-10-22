# SPDX-License-Identifier: GPL-3.0-or-later
"""The six llmtop pages (live, memory, system, requests, events, tensors) and the frame that wraps them."""

import os
import textwrap
import time
from collections import defaultdict

from . import model, prompts
from . import state as S
from .state import mib
from .ui import STYLE, C, ago, bar, clip, core_glyph, frame, gib, num, pad, plot, rate, rpad, shade, stack, tok, vlen

NAN = float("nan")
ICON = {"crit": ("◆", "bred"), "warn": ("▲", "byellow"), "info": ("●", "bcyan")}
DEC_EDGES, DEC_LABELS = (8000, 32000, 64000), ("<8k", "8-32k", "32-64k", "64k+")
DEVICES = ("all", "GPU", "CPU")


class View:
	PAGES = ("Live", "Memory", "System", "Requests", "Events", "Tensors")
	WINDOWS = ((60, "1m"), (300, "5m"), (900, "15m"), (3600, "1h"), (21600, "6h"), (86400, "24h"))

	def __init__(self, window=1):
		self.page, self.win, self.scroll, self.msel, self.rsel, self.level, self.paused, self.msg = 0, window, 0, 0, 0, 0, False, ""
		self.gh, self.tmode, self.dev, self.turns = 0, 0, 0, False  # graph height (0 = fit the terminal), tensor view mode, tensor device filter, nested turns
		self.live_prompts = False  # the Live page's bottom table: recent prompts instead of recent turns
		self.detail = False  # the Events page's detail toggle: show each event's detail line(s)
		self.cache, self.cache_sec = {}, 0

	@property
	def seconds(self):
		return self.WINDOWS[self.win][0]

	@property
	def label(self):
		return self.WINDOWS[self.win][1]


# ---- helpers -------------------------------------------------------------------------------------------------------------

def color(name):
	return getattr(C, name)


def clock(wall, long=False):
	if not wall:
		return "--:--:--"
	return time.strftime("%m-%d %H:%M" if long or time.time() - wall > 12 * 3600 else "%H:%M:%S", time.localtime(wall))


def mmss(seconds):
	seconds = int(max(0, seconds))
	return "%d:%02d" % (seconds // 60, seconds % 60) if seconds < 3600 else "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)


def pct(xs, q):
	xs = sorted(x for x in xs if x == x)
	return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else NAN


def f0(x, fmt="%.0f"):
	return "-" if x != x else fmt % x


def bucket(x, edges):
	return sum(1 for e in edges if x >= e)


def section(title, W, extra=""):
	"""A bold title and a dim rule across the page; `extra` is dim text after the title."""
	text = " %s%s%s%s " % (C.bold, title, C.reset, "   %s%s%s" % (C.grey, extra, C.reset) if extra else "")
	return "%s──%s%s%s%s" % (C.grey, C.reset, text, C.grey, "─" * max(2, W - 5 - vlen(text)) + C.reset)


def kvline(label, text):
	return "  %s%s%s %s" % (C.grey, pad(label, 15), C.reset, text)


def graph_w(W):
	return max(24, min(56, W - 72))


def series(c, view, snap, key, width):
	"""Values of a recorded series over the view's window, one per column (cached for the current second)."""
	sec = int(snap["t"])
	if sec != view.cache_sec:
		view.cache, view.cache_sec = {}, sec
	k = (key, view.win, width)
	if k not in view.cache:
		view.cache[k] = c.rings[key].cols(snap["t"], view.seconds, width, S.SERIES[key])
	return view.cache[k]


def scaled(vals, rows, floor=None, span=1.0, col="", fill=False):
	"""(plot lines, finite values, low, high) scaled to what the series did: a steady series is a flat line at mid-height."""
	fin = [v for v in vals if v == v]
	if not fin:
		return plot(vals, 0.0, 1.0, rows), fin, 0.0, 1.0
	lo, hi = (floor if floor is not None else min(fin)), max(fin)
	if hi - lo < span:
		if floor is None:
			lo = (lo + hi - span) / 2
		hi = lo + span
	return plot(vals, lo, hi, rows, col, floor is not None, fill), fin, lo, hi


def gchart(c, view, snap, key, width, rows, floor=None, span=1.0, col="", fill=False):
	pts = width * 2 if STYLE["mode"] == "braille" else width  # braille carries two data points per character
	return scaled(series(c, view, snap, key, pts), rows, floor, span, col, fill)


def block(c, view, snap, key, G, rows, label, now, fmt=num, unit="", floor=None, span=1.0, col="", fill=False, note="", vals=None):
	"""A framed chart. The label and current value sit beside the first line, the high of the scale is printed on the top border and the low on the
	bottom border, and ticks on the borders mark quarters of the time window. One-line charts have side bars only."""
	if vals is not None:
		G = (len(vals) + 1) // 2 if STYLE["mode"] == "braille" else len(vals)
	gr, fin, lo, hi = gchart(c, view, snap, key, G, rows, floor, span, col, fill) if vals is None else scaled(vals, rows, floor, span, col, fill)
	g, r = C.grey, C.reset
	f = frame()
	n = len(gr)
	if n == 1:
		axis = "%s – %s%s" % (fmt(lo), fmt(hi), unit) if fin else "no data yet"
		return ["  " + pad(label, 24) + rpad(now, 14) + "  " + g + f["v"] + r + pad(gr[0], G) + g + f["v"] + r + "  " + g + axis + r + ("   " + note if note else "")]
	ticks = {G * k // 4 for k in (1, 2, 3)}
	top = f["tl"] + "".join(f["tm"] if i in ticks else f["h"] for i in range(G)) + f["tr"]
	bottom = f["bl"] + "".join(f["bm"] if i in ticks else f["h"] for i in range(G)) + f["br"]
	lead = "  " + " " * 38 + "  "
	out = [lead + g + top + r + "  " + g + ((fmt(hi) + unit) if fin else "no data yet") + r]
	for i, line in enumerate(gr):
		left = pad(label, 24) + rpad(now, 14) if i == 0 else " " * 38
		out.append("  " + left + "  " + g + f["v"] + r + pad(line, G) + g + f["v"] + r + ("   " + note if note and i == n // 2 else ""))
	out.append(lead + g + bottom + r + "  " + g + ((fmt(lo) + unit) if fin else "") + r)
	return out


def thead(view, G):
	"""Column titles above the first chart of a group."""
	return "%s  %s%s   %s%s    high / low%s" % (C.grey, pad("last %s" % view.label, 24), rpad("now", 14), pad("◂ %s ago" % view.label, max(1, G - 5)), "now ▸", C.reset)


def paged(view, head, count, row, H, pin=None):
	"""A header followed by `count` lazily formatted rows, scrolled as one list. `pin` replaces the first row once the header has scrolled away."""
	total = len(head) + count
	view.scroll = max(0, min(view.scroll, max(0, total - H)))
	lo, hi = view.scroll, view.scroll + H
	out = head[lo:hi]
	a, b = max(0, lo - len(head)), max(0, hi - len(head))
	rows = [row(i) for i in range(a, min(b, count))]
	if pin and lo >= len(head) and rows:
		rows = [pin] + rows[1:]
	return out + rows


def ctx_of(r):
	return (r.get("cached") or 0) + r.get("pn", 0)


def bucket_stats(c, view, snap):
	"""Median throughput of similar turns: prefill and decode by context depth, per preset (cached per second)."""
	sec = int(snap["t"])
	if view.cache.get("bk_sec") != sec or view.cache.get("bk_len") != len(c.requests):
		pre, dec = defaultdict(list), defaultdict(list)
		for r in c.requests:
			if "pp" in r and r["pp"] == r["pp"] and r.get("pn", 0) >= 256:
				pre[(r["preset"], bucket(ctx_of(r), DEC_EDGES))].append(r["pp"])
			if "tg" in r and r["tg"] == r["tg"] and r.get("tn", 0) >= 20:
				dec[(r["preset"], bucket(r.get("kv", 0), DEC_EDGES))].append(r["tg"])
		view.cache["bk"], view.cache["bk_sec"], view.cache["bk_len"] = (pre, dec), sec, len(c.requests)
	return view.cache["bk"]


def prompt_groups(c, view, snap):
	"""Grouped prompts and the set of `id(turn)` for each prompt's first turn (cached per second)."""
	sec = int(snap["t"])
	gap_s = c.cfg.get("prompt_gap_s", S.CFG["prompt_gap_s"])
	if view.cache.get("pr_sec") != sec or view.cache.get("pr_len") != len(c.requests) or view.cache.get("pr_gap") != gap_s:
		ps = prompts.group(c.requests, gap_s)
		firsts = {id(p["turns"][0]) for p in ps if p["turns"]}
		view.cache["pr"], view.cache["pr_sec"], view.cache["pr_len"], view.cache["pr_gap"] = (ps, firsts), sec, len(c.requests), gap_s
	return view.cache["pr"]


def ref(xs):
	return S.median(xs) if len(xs) >= 3 else NAN


def rel(x, reference):
	"""Colour a throughput against the usual value for similar turns; dim grey when not compared."""
	if x != x:
		return C.grey
	if reference != reference or reference <= 0:
		return C.grey
	if x >= 1.2 * reference:
		return C.bcyan  # a turn far faster than usual is its own colour, not green
	return C.bgreen if x >= 0.8 * reference else C.byellow if x >= 0.5 * reference else C.bred


def legend():
	return "%s  t/s vs usual: %s■%s ≥120%%  %s■%s 80-120%%  %s■%s 50-80%%  %s■%s <50%%  %s■%s no data%s" % (C.grey, C.bcyan, C.grey, C.bgreen, C.grey, C.byellow, C.grey, C.bred, C.grey, C.grey, C.grey, C.reset)


def rank_colours(values):
	"""Colours for one column of the by-context tables: `values` is a list of (median, n_turns) in preset order and the
	return is one colour per entry. Cells with fewer than 3 turns are dim grey; of the rest, ranked fastest first and
	grouped into near-tie clusters (walking fastest to slowest, a cell joins the current cluster while within 5% of
	that cluster's fastest value), every cell in a cluster takes the colour its fastest member would get by rank: the
	fastest `k` clusters are green and the slowest `k` red (k = 1 when there are at most 4 ranked presets, else 2) with
	the middle yellow; a single ranked preset is plain."""
	ranked = [(med, i) for i, (med, n) in enumerate(values) if n >= 3 and med == med]
	out = [C.grey] * len(values)
	if len(ranked) <= 1:  # nothing to compare: a single ranked preset is plain, an empty column is all grey
		if ranked:
			out[ranked[0][1]] = ""
		return out
	ranked.sort(key=lambda t: t[0], reverse=True)
	k = 1 if len(ranked) <= 4 else 2
	pos = 0
	while pos < len(ranked):
		start = pos  # the rank of this cluster's fastest member
		first = ranked[pos][0]
		members = []
		while pos < len(ranked) and ranked[pos][0] >= 0.95 * first:
			members.append(ranked[pos][1])
			pos += 1
		col = C.bgreen if start < k else C.bred if start >= len(ranked) - k else C.byellow
		for i in members:
			out[i] = col
	return out


# ---- frame ---------------------------------------------------------------------------------------------------------------

def header(c, snap, view, W):
	tabs = " ".join("%s %d %s %s" % (C.rev if i == view.page else C.grey, i + 1, name, C.reset) for i, name in enumerate(View.PAGES))
	if not snap["router_up"] and snap.get("mode") is None:
		up = "%s● server not found%s" % (C.bred, C.reset)
	else:
		word = "server" if snap.get("mode") == "single" else "router"
		up = "%s● %s up%s" % (C.bgreen, word, C.reset) if snap["router_up"] else "%s● %s DOWN%s" % (C.bred, word, C.reset)
	pause = "   %sPAUSED%s" % (C.byellow + C.bold, C.reset) if view.paused else ""
	return " %sllmtop%s  %s  %s    %s    window %s%s%s%s" % (C.bold, C.reset, time.strftime("%H:%M:%S", time.localtime(snap["t"])), up, tabs, C.bold, view.label, C.reset, pause)


def banner(c, snap, n, W=120):
	alerts = c.alerts.ordered()
	if not alerts:
		return ["%s ✓ no alerts%s" % (C.bgreen, C.reset)]
	out = []
	for _key, a in alerts[:n]:
		icon, col = ICON[a["level"]]
		prefix = " %s%s %s%s %s%5s%s  " % (color(col), icon, a["level"].upper().ljust(4), C.reset, C.grey, ago(snap["t"] - a["since"]), C.reset)
		out.append(prefix + a["msg"])  # the headline only, one line, never wrapped
	if len(alerts) > n:
		out.append("%s   … %d more alert(s)%s" % (C.grey, len(alerts) - n, C.reset))
	return out


def notice_lines(snap, n=4):
	"""Grey notice lines, one per notice, prefixed `· `: at most n lines, the last saying how many more the Events page lists; n=None lists them all."""
	notices = snap.get("notices") or []
	if not notices:
		return []
	if n is not None and len(notices) > n:
		return ["  %s· %s%s" % (C.grey, s, C.reset) for s in notices[:n - 1]] + ["  %s+%d more (see the Events page)%s" % (C.grey, len(notices) - n + 1, C.reset)]
	return ["  %s· %s%s" % (C.grey, s, C.reset) for s in notices]


def footer(view, W):
	page = View.PAGES[view.page]
	keys = "1-6 or ←/→ page   [ ] time window   g graph height   s graph style   p pause   ↑↓ PgUp PgDn scroll"
	keys += {"Live": "   t prompts/turns", "Memory": "   m next model", "System": "", "Requests": "   m preset   t turns", "Events": "   f level filter   d detail", "Tensors": "   t layers/tensors   d device   m next model"}[page]
	return "%s %s   q quit%s%s" % (C.bold, keys, C.reset, "   " + view.msg if view.msg else "")


def render(c, snap, view, W, H):
	lines = [header(c, snap, view, W)]
	lines += banner(c, snap, 4 if view.page == 0 else 2, W)
	body_h = H - len(lines) - 1
	page = (live, memory, system, requests, events, tensors)[view.page]
	body = page(c, snap, view, W, body_h)
	lines += body[:body_h] + [""] * max(0, body_h - len(body))
	lines.append(footer(view, W))
	return [clip(ln, W - 1) for ln in lines[:H]]


# ---- page 1: live --------------------------------------------------------------------------------------------------------

def shared_note(ll, width=None):
	"""Sub-text for the llama-server shared row: expected, beyond, since loaded."""
	src = "expected" if ll.get("allow_src") == "measured" else "projected"
	exp = ("%s MiB %s" % (num(ll["allow"]), src)) if ll.get("allow_known") else "expected: unknown"
	beyond = ("+%s MiB beyond" % num(ll["over"])) if ll.get("over", 0) > 0 else ""
	since = ("{:+,.0f} since loaded".format(round(ll["shr"] - ll["floor"]))) if ll.get("floor_known") else ""

	terms = [x for x in (exp, beyond, since) if x]
	line = "  ·  ".join(terms)
	if width is None or len(line) <= width:
		return line
	terms = [x for x in (exp, beyond) if x]
	line = "  ·  ".join(terms)
	if len(line) <= width:
		return line
	exp_short = ("%s MiB" % num(ll["allow"])) if ll.get("allow_known") else "unknown"
	terms = [x for x in (exp_short, beyond) if x]
	return "  ·  ".join(terms)


def gpu_block(c, snap, view, W, gh):
	g, ll = snap["gpu"], snap["llama"]
	if not g:
		return ["%s no GPU telemetry (needs Windows performance counters)%s" % (C.grey, C.reset)]
	G, bw = graph_w(W), max(24, min(56, W - 70))
	fin = [v for v in series(c, view, snap, "budget", 30) if v == v]
	rest = max(0.0, g["ded"] - ll["ded"])
	span = " (%s – %s in this window)" % (num(min(fin)), num(max(fin))) if len(fin) > 1 and max(fin) - min(fin) > 1 else ""
	if g["vram"] > 0:
		used = g["ded"] / g["vram"]
		out = [section("GPU", W, "%s · %s MiB" % (g["name"], num(g["vram"])))]
		out.append("  VRAM  %s  %s%s / %s MiB  %.0f%%%s" % (stack([(ll["ded"], C.bcyan), (rest, C.blue)], g["vram"], bw, marker=g["budget"]), shade(used), num(g["ded"]), num(g["vram"]), 100 * used, C.reset))
	elif g["budget"] > 0:  # integrated and shared-memory adapters report 0 dedicated memory: scale the bar against the OS budget
		used = g["ded"] / g["budget"]
		out = [section("GPU", W, g["name"])]
		out.append("  VRAM  %s  %s%s / %s MiB of the OS budget (the adapter reports no dedicated memory)  %.0f%%%s" % (stack([(ll["ded"], C.bcyan), (rest, C.blue)], g["budget"], bw), shade(used), num(g["ded"]), num(g["budget"]), 100 * used, C.reset))
	else:
		out = [section("GPU", W, g["name"]), "  %sVRAM: the adapter reports no dedicated memory; in use %s MiB%s" % (C.grey, num(g["ded"]), C.reset)]
	out.append("        %s■%s llama-server %s MiB    %s■%s other apps %s MiB    %s┃%s OS budget %s MiB (DXGI)%s" % (C.bcyan, C.reset, num(ll["ded"]), C.blue, C.reset, num(rest), C.bred, C.reset, num(g["budget"]), span))
	if ll["present"]:
		grown = (ll["floor_known"] and ll["shr"] - ll["floor"] >= c.cfg["spill_mib"]) or (not ll["floor_known"] and ll["over"] >= c.cfg["spill_mib"])
		moving = any(k == "burst" and a["level"] == "crit" for k, a in c.alerts.active.items())
		out += ["", thead(view, G)]
		out += block(c, view, snap, "vram_llama", G, gh, "llama-server VRAM", num(ll["ded"]) + " MiB", num, " MiB", None, 512.0, C.bcyan)
		out += block(c, view, snap, "spill", G, gh, "llama-server shared", "%s%s MiB%s" % (C.bred + C.bold if moving else C.byellow + C.bold if grown else "", num(ll["shr"]), C.reset), num, " MiB", None, 512.0, C.bred if moving else C.byellow, False)
		note = shared_note(ll, max(0, W - 3))
		if note:
			out.append("%s  %s%s" % (C.grey, note, C.reset))
		eng = "  ".join("%s %.0f%%" % (k, v) for k, v in sorted(ll["eng"].items(), key=lambda kv: -kv[1]) if v >= 0.5)
		out += block(c, view, snap, "gpu_llama", G, gh, "GPU load", "%.0f%%" % max(ll["eng"].values(), default=0.0), num, "%", 0.0, 100.0, C.bgreen, True, eng or "idle")
	if snap["others"]:
		out.append("%s  other apps  %s%s" % (C.grey, " · ".join("%s %s MiB%s%s" % (o["exe"], num(o["ded"]), " (GPU %.0f%%)" % o["eng"] if o["eng"] >= 5 else "", ", shared %s MiB" % num(o["shr"]) if o["shr"] >= 64 else "") for o in snap["others"][:4]), C.reset))
	return out


def pm_unavailable(ll):
	"""`unavailable (reason)` for llama-server's RAM figures when proc_mem failed, else None."""
	err = ll.get("pm_error")
	return "%sunavailable (%s)%s" % (C.grey, err, C.reset) if err else None


def ram_block(c, snap, view, W, gh):
	rm, ll = snap["ram"], snap["llama"]
	if not rm:
		return []
	G, bw = graph_w(W), max(10, min(24, W // 8))
	used = (rm["total"] - rm["avail"]) / rm["total"]
	cm = rm["commit"] / rm["commit_max"]
	out = [section("RAM", W, "%s · page file %s" % (gib(rm["total"]), ", ".join("%s %s" % (p["name"], gib(p["size"])) for p in rm["pagefiles"]) or "none"))]
	out.append("  in use %s %s%s%s    commit %s %s%s / %s%s at max page file    file cache %s    free %s" % (
		bar(used, bw, shade(used, 0.85, 0.95)), shade(used, 0.85, 0.95), gib(rm["total"] - rm["avail"]), C.reset, bar(cm, bw, shade(cm, 0.85, 0.95)), shade(cm, 0.85, 0.95), gib(rm["commit"]), gib(rm["commit_max"]), C.reset, gib(rm["standby"]), gib(rm["avail"])))
	out += ["", thead(view, G)]
	if ll["present"]:
		un = pm_unavailable(ll)
		out += block(c, view, snap, "llama_ws", G, gh, "llama-server RAM", un or gib(ll["ws"]), gib, "", None, 1024.0, C.bcyan, False, "" if un else "commit %s · %.1f cores" % (gib(ll["commit"]), ll["cpu"]))
	out += block(c, view, snap, "ram_avail", G, gh, "RAM available", gib(rm["avail"]), gib, "", None, 2048.0, C.bgreen)
	out += block(c, view, snap, "pages_in", G, gh, "disk page-ins per s", "%.0f" % rm["pages_in"], num, "", 0.0, 100.0, C.byellow, True, "page-outs %.0f/s" % rm["pages_out"])
	return out


def cpu_block(c, snap, view, W, gh):
	cp = snap.get("cpu")
	if not cp:
		return []
	ll = snap.get("llama") or {}
	G = graph_w(W)
	u, mhz, q, lcpu = cp.get("cpu", NAN), cp.get("cpu_mhz", NAN), cp.get("cpu_queue", NAN), ll.get("cpu", NAN)
	bits = []
	if lcpu == lcpu:
		bits.append("llama-server %.1f cores" % lcpu)
	if mhz == mhz:
		bits.append("%.1f GHz" % (mhz / 1000.0))
	if q == q:
		bits.append("queue %.0f" % q)
	val = "%.0f%%" % min(100.0, max(0.0, u)) if u == u else "-"
	return block(c, view, snap, "cpu", G, gh, "CPU", val, num, "%", 0.0, 100.0, C.bcyan, True, " · ".join(bits))


def last_request(c, preset):
	for r in reversed(c.requests):
		if r["preset"] == preset:
			return r
	return None


def model_block(c, snap, view, preset, p, W, gh):
	flags = c.flags.get(preset, ({}, ""))[0]
	meta, G = p["meta"], graph_w(W)
	bits = [meta.get("ftype", "").replace(" - ", " ")] if meta.get("ftype") else []
	if meta.get("n_params"):
		bits.append("%.1fB params" % (meta["n_params"] / 1e9))
	if flags.get("ctx-size"):
		bits.append("ctx %s" % num(float(flags["ctx-size"])))
	if flags.get("cache-type-k"):
		bits.append("KV %s/%s" % (flags["cache-type-k"], flags.get("cache-type-v", flags["cache-type-k"])))
	if flags.get("spec-type") or flags.get("model-draft"):
		bits.append("%s x%s" % (flags.get("spec-type", "draft"), flags.get("spec-draft-n-max", "?")))
	if flags.get("reasoning-effort") or flags.get("reasoning"):
		bits.append("reasoning %s" % flags.get("reasoning-effort", flags.get("reasoning")))
	asm = c.follow.get(preset, (None, None))[1]
	sess = asm.session() if asm else None
	if sess and sess.get("wall"):
		bits.append("up %s" % ago(snap["t"] - sess["wall"]))
	out = [section(C.bcyan + preset + C.reset, W, " · ".join(bits))]
	s = p["slot"]
	if p["status"] == "loading":
		out.append("  %sloading… VRAM %s so far%s" % (C.byellow, gib(snap["llama"]["ded"]), C.reset))
		return out
	if not s:
		err = p.get("slot_error")
		out.append("  %s%s%s" % (C.grey, "slot data unavailable: %s" % err if err else "waiting for slot data…", C.reset))
		return out
	phase = p["phase"]
	if phase == "prefill":
		frac = s["done"] / max(1, s["todo"])
		eta = "   ETA %s at this rate" % mmss(p["eta"]) if p["eta"] == p["eta"] else ""
		out.append("  %sPREFILL%s  task %s, %s     %s / %s new tokens  %s %.0f%%   %s t/s%s" % (C.bgreen + C.bold, C.reset, s["task"], mmss(p["elapsed"]), tok(s["done"]), tok(s["todo"]), bar(frac, 24, C.bgreen), 100 * frac, rate(p["rate_pre"]).strip(), eta))
	elif phase in ("starting", "decode"):
		budget = s["decoded"] + s["remain"]
		out.append("  %sDECODE%s   task %s, %s     %s tokens, %s t/s now     %s of the %s-token limit" % (C.bgreen + C.bold, C.reset, s["task"], mmss(p["elapsed"]), tok(s["decoded"]), rate(p["rate_dec"], 1).strip(), bar(s["decoded"] / max(1, budget), 10, C.cyan), tok(budget)))
	else:
		last = last_request(c, preset)
		tail = ""
		if last:
			tail = "   last request %s ago: %s new + %s cached prompt tokens, %s generated" % (ago(snap["t"] - (last.get("wall") or snap["t"])), tok(last.get("pn", 0)), "-" if last.get("cached", 0) is None else tok(last.get("cached", 0)), tok(last.get("tn", 0)))
		out.append("  %sidle%s%s" % (C.grey, C.reset, tail))
	frac = p["kv"] / max(1, s["n_ctx"])
	extra = "     prompt %s = %s cached (%.0f%%) + %s new" % (tok(s["n_prompt"]), tok(s["cached"]), 100 * s["cached"] / max(1, s["n_prompt"]), tok(s["todo"])) if phase != "idle" else "     %safter the last request%s" % (C.grey, C.reset)
	out.append("  context  %s / %s tokens  %s %s%.1f%%%s%s" % (tok(p["kv"]), tok(s["n_ctx"]), bar(frac, 24, shade(frac)), shade(frac), 100 * frac, C.reset, extra))
	last = last_request(c, preset)
	if last and last.get("acc") is not None:
		out.append("  %sdraft model  accepted %.1f%%, mean accepted length %.2f (last request);  slot pick: %s%s%s" % (C.grey, 100 * last["acc"], NAN if last.get("mlen") is None else last["mlen"], last.get("sel", "-"), " %.0f%% similar" % (100 * last["sim"]) if last.get("sim") else "", C.reset))
	prm = s["params"]
	if prm and phase != "idle":
		out.append("%s  sampler  temp %s, top-k %s, top-p %.2f, min-p %s, max_tokens %s, %s%s" % (C.grey, round(prm.get("temperature", 0), 2), prm.get("top_k"), prm.get("top_p", 0), prm.get("min_p"), tok(prm.get("max_tokens", 0)), prm.get("reasoning_format", ""), C.reset))
	out += ["", thead(view, G)]
	out += block(c, view, snap, "dec", G, gh, "decode t/s", rate(p["rate_dec"], 1).strip() if p["rate_dec"] == p["rate_dec"] else "-", num, "", 0.0, 20.0, C.bgreen, True)
	out += block(c, view, snap, "pre", G, gh, "prefill t/s", rate(p["rate_pre"]).strip() if p["rate_pre"] == p["rate_pre"] else "-", num, "", 0.0, 100.0, C.bcyan, True)
	out += block(c, view, snap, "ctx", G, gh, "context tokens", tok(p["kv"]), tok, "", 0.0, float(s["n_ctx"]), C.byellow, False, "of %s" % tok(s["n_ctx"]))
	out += block(c, view, snap, "busy", G, 1, "busy", "yes" if phase != "idle" else "no", lambda v: "yes" if v else "no", "", 0.0, 1.0, C.bgreen, True)
	return out


def spill_of(c, r):
	"""Largest llama-server shared-memory figure seen while the request ran (NaN if llmtop had no data then)."""
	if "spill" not in r:
		r["spill"] = c.rings["spill"].span_max(r["wall0"], r["wall"]) if r.get("wall0") and r.get("wall") else NAN
	return r["spill"]


def ktok(n):
	return "%dk" % round(n / 1000) if n >= 1000 else str(int(n))


def row_notes(c, turns):
	notes = []
	compactions = 0
	ev = 0
	for i, r in enumerate(turns):
		if i > 0 and prompts.is_compaction(turns[i - 1], r):
			compactions += 1
		elif r.get("sel") == "LRU" and (r.get("evicted") or 0) > 0:
			ev = max(ev, r.get("evicted", 0))  # a compaction's eviction is by design, not a cache reset
	if compactions:
		notes.append("%scompacted%s%s" % (C.grey, " ×%d" % compactions if compactions > 1 else "", C.reset))
	if ev > 0:
		notes.append("%scache reset %s%s" % (C.byellow, ktok(ev), C.reset))
	if any(r.get("trunc") for r in turns):
		notes.append(C.bred + "cut off" + C.reset)
	if c.allow_known:
		sps = [spill_of(c, r) - c.allow for r in turns if spill_of(c, r) == spill_of(c, r)]
		sp = max(sps, default=NAN)
		if sp == sp and sp >= c.cfg["spill_mib"]:
			notes.append("%sshared +%s%s" % (C.byellow, gib(sp), C.reset))
	if any(r.get("reallocs") for r in turns):
		notes.append(C.byellow + "resized" + C.reset)
	return "  ".join(notes)


PROMPT_COLS = (("time", 12, "l"), ("preset", 24, "l"), ("turns", 5, "r"), ("context", 8, "r"), ("new", 8, "r"), ("hit", 5, "r"), ("prefill", 10, "r"), ("gen", 7, "r"), ("decode", 10, "r"), ("draft", 5, "r"), ("total", 6, "r"))
DROP_ORDER = ("gen", "total", "turns")  # dropped in this order when the table is wider than the page; time, context, new, hit, prefill, decode and draft are never dropped


def prompt_layout(W, presets, with_preset, with_turns=True):
	"""The preset column's width, the columns dropped so the request table fits in W, and whether the header's "notes"
	label is shown: the preset column sizes to the longest preset name shown (24 at most, 6 at least) and is omitted when
	one preset is shown; DROP_ORDER columns are dropped in order until the header fits, and if it still would not, the
	"notes" label is omitted rather than clipped."""
	pw = 0 if not with_preset else min(24, max(6, max((len(p) for p in presets), default=0)))
	drop = set()

	def fits():
		width, first = 2, True
		for name, w, _ in PROMPT_COLS:
			if name in drop or (name == "preset" and not with_preset) or (name == "turns" and not with_turns):
				continue
			width += (pw if name == "preset" else w) + (0 if first else 2)
			first = False
		return width + 7 <= W  # the header's "  notes" tail

	for name in DROP_ORDER:
		if fits():
			break
		drop.add(name)
	return pw, drop, fits()


def pct_cell(x):
	"""A share as a whole percentage, uncoloured; '-' when unknown (NaN or None)."""
	return "-" if x is None or x != x else "%.0f%%" % (100 * x)


def req_line(cells, with_preset, tail="", with_turns=True, pw=24, drop=()):
	parts = []
	for (name, width, align), cell in zip(PROMPT_COLS, cells, strict=True):
		if name in drop:
			continue
		if not with_preset and name == "preset":
			continue
		if not with_turns and name == "turns":
			continue
		parts.append(pad(cell, pw if name == "preset" else width) if align == "l" else rpad(cell, pw if name == "preset" else width))
	return "  " + "  ".join(parts) + ("  " + tail if tail else "")


def req_header(with_preset, with_turns=True, pw=24, drop=(), notes=True):
	return C.grey + req_line([name for name, _, _ in PROMPT_COLS], with_preset, "notes" if notes else "", with_turns=with_turns, pw=pw, drop=drop) + C.reset


def prompt_ratios(p, stats):
	"""Compare a prompt against the time its turns would usually take: for each metric, sum pn_i / ref_i (prefill) or
	tn_i / ref_i (decode) over the turns that have a bucket reference, and divide by the same turns' prefill (or decode)
	time; ref_i is the preset's median for that turn's own bucket, the same reference the turn row uses, with the same
	256+ / 20+ cutoffs. Each result is NaN (grey) when the referenced turns hold under half the prompt's new (or
	generated) tokens, so a prompt is only compared when its turns' usual times cover most of it."""
	pre, dec = stats
	exp_p, act_p, pn_ref = 0.0, 0.0, 0
	exp_d, act_d, tn_ref = 0.0, 0.0, 0
	for r in p["turns"]:
		pn, tn = r.get("pn", 0), r.get("tn", 0)
		ctx = ctx_of(r)
		kv = r["kv"] if "kv" in r else ctx + tn
		if pn >= 256:
			ref_p = ref(pre.get((r["preset"], bucket(ctx, DEC_EDGES)), []))
			if ref_p == ref_p and ref_p > 0:
				exp_p += pn / ref_p
				act_p += prompts._pms(r) / 1000.0
				pn_ref += pn
		if tn >= 20:
			ref_d = ref(dec.get((r["preset"], bucket(kv, DEC_EDGES)), []))
			if ref_d == ref_d and ref_d > 0:
				exp_d += tn / ref_d
				act_d += prompts._tms(r) / 1000.0
				tn_ref += tn
	pp = exp_p / act_p if act_p > 0 and pn_ref >= 0.5 * p["pn"] else NAN
	tg = exp_d / act_d if act_d > 0 and tn_ref >= 0.5 * p["tn"] else NAN
	return pp, tg


def prompt_row(c, p, stats, with_preset, pw=24, drop=()):
	pp, tg = p["pp"], p["tg"]
	kv = p["kv"]
	pp_ratio, tg_ratio = prompt_ratios(p, stats)
	cells = [
		clock(p.get("wall")),
		p["preset"][:24],
		str(p["n"]),
		tok(kv),
		tok(p["pn"]),
		pct_cell(p["hit"]),
		"%s%s t/s%s" % (rel(pp_ratio, 1.0), "-" if pp != pp else "%.0f" % pp, C.reset),
		tok(p["tn"]),
		"%s%s t/s%s" % (rel(tg_ratio, 1.0), "-" if tg != tg else "%.1f" % tg, C.reset),
		pct_cell(p["acc"]),
		mmss(p["ms"] / 1e3),
	]
	return req_line(cells, with_preset, row_notes(c, p["turns"]), with_turns=True, pw=pw, drop=drop)


def turn_row(c, r, stats, with_preset, mark="  ", with_turns=True, pw=24, drop=(), nested=False):
	pre, dec = stats
	pp, tg = r.get("pp", NAN), r.get("tg", NAN)
	ctx = ctx_of(r)
	kv = r["kv"] if "kv" in r else ctx + r.get("tn", 0)
	pp_ref = ref(pre.get((r["preset"], bucket(ctx, DEC_EDGES)), [])) if r.get("pn", 0) >= 256 else NAN
	tg_ref = ref(dec.get((r["preset"], bucket(kv, DEC_EDGES)), [])) if r.get("tn", 0) >= 20 else NAN
	w = r.get("wall")
	ts = time.strftime("%H:%M:%S", time.localtime(w)) if w else "--:--:--"
	if mark == "▸ ":
		t_cell = "%s▸ %s%s" % (C.bcyan, C.reset, ts)
	elif mark.strip():
		t_cell = "%s%s%s%s" % (C.grey, mark, C.reset, ts)
	else:
		t_cell = mark + ts
	cached, pn = r.get("cached"), r.get("pn", 0)
	hit = cached / (cached + pn) if cached is not None and cached + pn > 0 else NAN
	cells = [
		t_cell,
		"" if nested else r["preset"][:24],  # a nested turn's prompt row names the preset
		"",
		tok(kv),
		tok(pn),
		pct_cell(hit),
		"%s%s t/s%s" % (rel(pp, pp_ref), "-" if pp != pp else "%.0f" % pp, C.reset),
		tok(r.get("tn", 0)),
		"%s%s t/s%s" % (rel(tg, tg_ref), "-" if tg != tg else "%.1f" % tg, C.reset),
		pct_cell(r.get("acc")),
		mmss(r.get("ms", 0) / 1e3),
	]
	return req_line(cells, with_preset, row_notes(c, (r,)), with_turns=with_turns, pw=pw, drop=drop)


def req_row(c, r, stats, with_preset):
	return turn_row(c, r, stats, with_preset, mark="  ", with_turns=False)


def recent_requests(c, snap, view, W, n, preset=None):
	rs = [r for r in c.requests if preset is None or r["preset"] == preset]
	if not rs or n < 4:
		return []
	st = bucket_stats(c, view, snap)
	_, firsts = prompt_groups(c, view, snap)
	pw, drop, notes = prompt_layout(W, {r["preset"] for r in rs}, preset is None, with_turns=False)
	return [section("recent turns", W, "(t: prompts)"), legend(), req_header(preset is None, with_turns=False, pw=pw, drop=drop, notes=notes)] + [
		turn_row(c, r, st, preset is None, mark="▸ " if id(r) in firsts else "  ", with_turns=False, pw=pw, drop=drop) for r in reversed(rs[-(n - 3):])
	]


def recent_prompts(c, snap, view, W, n, preset=None):
	ps, _ = prompt_groups(c, view, snap)
	ps = [p for p in ps if preset is None or p["preset"] == preset]
	if not ps or n < 4:
		return []
	st = bucket_stats(c, view, snap)
	pw, drop, notes = prompt_layout(W, {p["preset"] for p in ps}, preset is None, with_turns=True)
	return [section("recent prompts", W, "(t: turns)"), legend(), req_header(preset is None, with_turns=True, pw=pw, drop=drop, notes=notes)] + [
		prompt_row(c, p, st, preset is None, pw=pw, drop=drop) for p in reversed(ps[-(n - 3):])
	]


def recent_events(c, W, n):
	if n < 3:
		return []
	evs = [e for e in c.events if not (e["kind"] == "alert" and e["level"] == "info" and e["msg"].startswith("cleared")) and not (e["kind"] in ("log", "router") and e["level"] == "info")][-(n - 1):]
	out = [section("recent events", W)]
	for e in reversed(evs):
		icon, col = ICON[e["level"]]
		out.append("  %s  %s%s%s %s" % (clock(e["wall"]).ljust(8), color(col), icon, C.reset, e["msg"]))
	return out


def live(c, snap, view, W, H):
	active = [(n, p) for n, p in snap["presets"].items() if p["status"] in ("loaded", "loading")]
	notices = notice_lines(snap)

	def build(gh):
		out = gpu_block(c, snap, view, W, gh) + [""] + ram_block(c, snap, view, W, gh) + cpu_block(c, snap, view, W, gh)
		if not active:
			out += ["", "%s no model loaded (models load on the first request)%s" % (C.grey, C.reset)]
		for preset, p in active:
			out += [""] + model_block(c, snap, view, preset, p, W, gh)
		return out

	choices = [view.gh] if view.gh else [3, 2, 1]
	for gh in choices:  # the tallest graphs that still leave room for about eight request rows
		out = notices + build(gh)
		if H - len(out) >= (13 if gh > 1 else 16) or gh == choices[-1]:
			break
	room = H - len(out) - 1
	ev_n = 5 if room >= 24 else 0
	req_n = max(0, min(20, room - (ev_n + 1 if ev_n else 0)))
	preset = active[0][0] if len(active) == 1 else None
	reqs = recent_prompts(c, snap, view, W, req_n, preset) if view.live_prompts else recent_requests(c, snap, view, W, req_n, preset)
	if reqs:
		out += [""] + reqs
	evs = recent_events(c, W, ev_n if H - len(out) - 1 >= ev_n else 0)
	if evs:
		out += [""] + evs
	return out


# ---- model selection shared by the Memory and Tensors pages --------------------------------------------------------------

def pick_model(c, snap, view):
	known = list(c.flags)
	loaded = [n for n, p in snap["presets"].items() if p["status"] == "loaded"]
	order = loaded + [n for n in known if n not in loaded]
	if not order:
		return None, [], loaded
	name = order[view.msel % len(order)]
	c.ensure_map(name)
	return name, order, loaded


def no_models(c):
	url = c.cfg.get("router")
	return "no models reported by the server at %s" % url if url else "no server found"


def cache_limit(m):
	return "disabled" if m["cache_ram"] == 0 else "no limit" if m["cache_ram"] < 0 else gib(m["cache_ram"])


def freeable(m, held):
	"""Arithmetic on the derived KV size: what changing the preset would give back."""
	kv, parts = m["kv"], []
	per_tok = (kv["full"] + kv["mtp"]) / m["ctx"] if m["ctx"] else 0
	if per_tok > 0:
		parts.append("every 8,192 tokens of ctx-size ≈ %s" % mib(per_tok * 8192))
	bk, bv = model.BPE.get(m["cache_k"]), model.BPE.get(m["cache_v"])
	if bk and bv and bk + bv > 2 * 0.5625 + 0.01:
		parts.append("KV cache as q4_0 ≈ %s" % mib((kv["full"] + kv["swa"] + kv["mtp"]) * (1 - 2 * 0.5625 / (bk + bv))))
	if held:
		parts.append("other apps hold %s" % mib(held))
	return parts


def cpu_reasons(m):
	"""[(tag, tensors, MiB, example names)] for everything kept on the host."""
	by = {}
	for t in m["roster"]:
		if t["dev"] == "CPU":
			n, size, names = by.get(t["why"], (0, 0.0, []))
			by[t["why"]] = (n + 1, size + t["mib"], names + [t["name"]] if len(names) < 3 else names)
	return [(why, n, size, names) for why, (n, size, names) in sorted(by.items(), key=lambda kv: -kv[1][1])]


# ---- page 2: memory ------------------------------------------------------------------------------------------------------

def host_lines(c, snap, name, m, loaded):
	"""llama-server's own memory: measured values, plus the configured limits. Nothing here says what the memory is used for."""
	ll = snap["llama"]
	host = m["fit"]["host"] if m["fit"] else {"model": m["weights"]["cpu"], "compute": 0, "self": m["weights"]["cpu"]}
	vm = ll["vm"] if name in loaded and ll["present"] else None
	out = []
	for tag, n, size, names in cpu_reasons(m):
		out.append(kvline("on the CPU", "%s %s MiB  %s[%s]%s" % (", ".join(names) + (" …" if n > len(names) else ""), num(size), C.byellow, tag, C.reset)))
	if m["fit"]:
		out.append(kvline("compute buffers", "%s MiB on the host, as projected by llama.cpp" % num(host["compute"])))
	out.append(kvline("configured", "cache-ram %s · %d context checkpoints per slot" % (cache_limit(m), m["ckpt_max"])))
	if not (name in loaded and ll["present"]):
		return out
	un = pm_unavailable(ll)
	out.append(kvline("working set", un or ("%s   private %s · commit %s · %.1f cores" % (gib(ll["ws"]), gib(ll["pws"]), gib(ll["commit"]), ll["cpu"]))))
	if vm:
		out.append(kvline("committed", "private %s · mapped files %s · images %s · reserved %s" % (gib(vm["private"]), gib(vm["mapped"]), gib(vm["image"]), gib(vm["reserved"]))))
		if vm["files"]:
			out.append(kvline("mapped files", " · ".join("%s %s" % (os.path.basename(f), gib(v) if v >= 1024 else mib(v)) for f, v in sorted(vm["files"].items(), key=lambda kv: -kv[1])[:3])))
		out.append(kvline("private commit", "%s; GPU resident + shared is %s; difference %s" % (gib(vm["private"]), gib(ll["ded"] + ll["shr"]), gib(vm["private"] - ll["ded"] - ll["shr"]))))
		if m["cache_ram"] > 0:
			rest = vm["private"] - ll["ded"] - ll["shr"] - host["self"]
			out.append(kvline("deduction", "%s of private commit is left after llama.cpp's projected host weights and buffers (%s). The prompt cache cannot exceed cache-ram (%s, per --help), %s" % (
				gib(rest), mib(host["self"]), gib(m["cache_ram"]), "so at least %s of it is something else" % gib(rest - m["cache_ram"]) if rest > m["cache_ram"] else "so this is consistent with it being all prompt cache, which does not prove it")))
	else:
		err = ll.get("vm_error")
		out.append(kvline("committed", "%s%s%s" % (C.grey, "unavailable (%s)" % err if err else "address-space walk running…", C.reset)))
	return out


def vram_bar(g, rows, bw, pal):
	"""The Memory page's VRAM bar: scaled against dedicated VRAM, or against the OS budget when the adapter has no dedicated memory (UMA), like the Live page."""
	parts = [(r[1], pal[i % len(pal)]) for i, r in enumerate(rows) if r[1] > 0]
	if g.get("uma"):
		return "  " + stack(parts, g["budget"], bw) + "  %s┃%s shared GPU memory (no dedicated VRAM) %s (DXGI)" % (C.bred, C.reset, gib(g["budget"]))
	return "  " + stack(parts, g["vram"], bw, marker=g["budget"]) + "  %s┃%s OS budget %s (DXGI, as seen by llmtop)" % (C.bred, C.reset, gib(g["budget"]))


def memory(c, snap, view, W, H):
	name, order, loaded = pick_model(c, snap, view)
	if name is None:
		return ["", "%s %s%s" % (C.grey, no_models(c), C.reset)]
	entry = c.mem.get(name, {})
	g, ll = snap["gpu"], snap["llama"]
	head = section("MEMORY", W, "%s · %s   (m: next model, %d of %d)" % (name, "loaded" if name in loaded else "not loaded, projection only", order.index(name) + 1, len(order)))
	if entry.get("state") == "pending":
		return [head, "", "  %sanalysing the model (GGUF header, llama-fit-params)…%s" % (C.byellow, C.reset)]
	if entry.get("state") == "error":
		return [head, "", "  %scould not analyse: %s%s" % (C.bred, entry["error"], C.reset)]
	m = entry["map"]
	on = name in loaded and ll["present"]
	measured = ll["ded"] if on else None
	kinds = {}
	for ly in m["layers"].values():
		kinds[ly["kind"]] = kinds.get(ly["kind"], 0) + 1
	layout = " + ".join("%d %s" % (n, {"full": "full attention", "swa": "sliding window", "linear": "linear/SSM", "mtp": "MTP", "other": "other"}[k]) for k, n in kinds.items())
	out = [head, "%s  %s · %s · %d blocks: %s%s%s" % (C.grey, m["name"], m["arch"], m["n_layer"], layout, " · MoE %d of %d experts" % (m["n_expert_used"], m["n_expert"]) if m["n_expert"] else "", C.reset)]
	if m["verified"]:
		out.append("%s  ✓ weights, KV and state agree with llama.cpp's own projection (llama-fit-params, within 3%%)%s" % (C.grey, C.reset))
	elif not m["fit"]:
		out.append("%s  ▲ llama-fit-params gave no projection: weights, KV and state come from the GGUF and the flags alone%s" % (C.byellow, C.reset))
	out += ["%s  ▲ %s%s" % (C.byellow, ch, C.reset) for ch in m["checks"]]
	out += ["%s  ▲ assumed: %s%s" % (C.byellow, a, C.reset) for a in m["assumed"]]
	cal = c.calib.get(c.calib_key(entry["key"]))
	rows = model.memory_rows(m, measured, cal["overhead"] if cal and measured is None else None)
	est = sum(r[1] for r in rows if not r[0].startswith(("unattributed", "projection above")))
	total = measured if measured is not None else est
	pal = [C.bcyan, C.bgreen, C.byellow, C.magenta, C.blue, C.red, C.green, C.cyan, C.yellow, C.bred]
	bw = max(30, min(100, W - 36))
	out += ["", section("VRAM", W, "resident %s · shared %s" % (gib(ll["ded"]), gib(ll["shr"])) if on else "projected resident %s%s" % (gib(est), " (includes the overhead learned on an earlier load)" if cal else ", no overhead learned yet for this configuration"))]
	if g:
		left = g["budget"] - total
		held = sum(o["ded"] for o in snap["others"])
		out.append(vram_bar(g, rows, bw, pal))
		kind = "resident" if on else "projected resident"
		out.append("  %s%s %s %s the OS budget%s" % (C.bgreen if left >= c.cfg["near_budget_mib"] else C.byellow if left > 0 else C.bred, kind, gib(total), "leaves %s under" % mib(left) if left >= 0 else "is %s over" % mib(-left), C.reset))
		growth = ll["shr"] - ll["floor"] if on and ll["floor_known"] else 0
		excess = ll["over"] if on and ll["allow_known"] else 0
		if on:
			bits = ["shared %s" % mib(ll["shr"])]
			if ll["allow_known"]:
				src_word = "expected" if ll["allow_src"] == "measured" else "projected"
				bits.append("%s %s" % (mib(ll["allow"]), src_word))
			else:
				bits.append("expected: unknown")
			if ll["over"] > 0:
				bits.append("+%s beyond" % mib(ll["over"]))
			if ll["floor_known"]:
				bits.append("{:+,.0f} since loaded".format(round(growth)))
			max_w = max(0, W - 3)
			line = "  ·  ".join(bits)
			if len(line) > max_w:
				bits_no_since = [b for b in bits if not b.endswith("since loaded")]
				line = "  ·  ".join(bits_no_since)
				if len(line) > max_w:
					bits_short = []
					for b in bits_no_since:
						if ll["allow_known"] and b == "%s %s" % (mib(ll["allow"]), src_word):
							bits_short.append(mib(ll["allow"]))
						else:
							bits_short.append(b)
					line = "  ·  ".join(bits_short)
			out.append("  " + line)
			if ll["allow_known"]:
				src_expl = "measured on an earlier load with resident + shared at least %s under the budget" % mib(c.cfg["idle_headroom_mib"]) if ll["allow_src"] == "measured" else "projected by llama.cpp, not verified for this preset"
				out.append("%s  %s%s" % (C.grey, src_expl, C.reset))
		if left < c.cfg["near_budget_mib"] or excess >= c.cfg["spill_mib"] or growth >= c.cfg["spill_mib"]:
			parts = freeable(m, held)
			if parts:
				out.append("%s  freeable: %s%s" % (C.grey, "  ·  ".join(parts), C.reset))
	if cal and cal.get("spilled") and not on:
		out.append("%s  the learned overhead was measured while shared memory was above the threshold, so resident VRAM may have understated demand%s" % (C.grey, C.reset))
	out.append("")
	for i, (label, mib_, note) in enumerate(rows):
		out.append("  %s■%s %s %9s MiB %5.1f%%  %s%s%s" % (pal[i % len(pal)], C.reset, pad(label, 30), num(mib_), 100 * mib_ / total if total else 0, C.grey, note, C.reset))
	out += ["", section("HOST RAM", W, "llama-server pid %s, measured %s ago" % (ll["vm"]["pid"], ago(snap["t"] - ll["vm"]["at"])) if on and ll["vm"] else "projection")]
	out += host_lines(c, snap, name, m, loaded)
	cols = max(1, (W - 8) // 7)
	out += ["", section("LAYERS", W, "%sA%s full attention  %sS%s sliding window  %sL%s linear/SSM  %sM%s MTP   %s■%s GPU  %s■%s CPU  %s■%s mixed   number = MiB   (6 lists every tensor)" % (
		C.grey, C.reset, C.grey, C.reset, C.grey, C.reset, C.grey, C.reset, C.bgreen, C.reset, C.byellow, C.reset, C.magenta, C.reset))]
	letter = {"full": "A", "swa": "S", "linear": "L", "mtp": "M", "other": "·"}
	cells = []
	for _i, ly in sorted(m["layers"].items()):
		col = C.bgreen if ly["gpu"] >= ly["mib"] - 0.01 else C.byellow if ly["gpu"] < 0.01 else C.magenta
		cells.append("%s%s%4.0f%s " % (col, letter[ly["kind"]], ly["mib"], C.reset))
	for i in range(0, len(cells), cols):
		out.append("  %s%3d%s  %s" % (C.grey, i, C.reset, " ".join(cells[i:i + cols])))
	return paged(view, out, 0, None, H)


# ---- page 3: system ------------------------------------------------------------------------------------------------------

def cache_size(b):
	if b >= 2**20 and b % 2**20 == 0:
		return "%d MiB" % (b // 2**20)
	if b >= 2**20 and (b * 2) % 2**20 == 0:
		return "%.1f MiB" % (b / 2**20)
	return "%d KiB" % (b // 1024)


def system(c, snap, view, W, H):
	rm, ll, cp = snap.get("ram"), snap.get("llama") or {}, snap.get("cpu") or {}
	info = getattr(c, "cpu_info", None) or {}
	G, gh, bw = graph_w(W), view.gh or 2, max(30, min(80, W - 40))
	out = []
	if info or cp:
		err = info.get("error")
		if not err and info.get("name"):
			split = " (%dP + %dE)" % (info["p_cores"], info["e_cores"]) if info.get("e_cores") else ""
			sub = "%s · %d cores%s, %d threads" % (info["name"], info.get("cores", 0), split, info.get("threads", 0))
		else:
			sub = ""
		out.append(section("CPU", W, sub))
		if err:
			out.append("  %s%s%s" % (C.grey, err, C.reset))
		cores = [v for v in (cp.get("cpu_cores") or []) if v == v]
		if cores:
			cells = []
			for v in cores:
				u = min(100.0, max(0.0, v))
				col = C.bred if u > 85.0 else C.byellow if u >= 50.0 else C.bgreen
				cells.append("%s%s%s" % (col, core_glyph(u), C.reset))
			step = max(1, W - 4)
			for i in range(0, len(cells), step):
				out.append("  " + "".join(cells[i:i + step]))
		if not err:
			for ca in info.get("caches") or ():
				lbl = "L%d%s" % (ca["level"], {"d": "d", "i": "i", "data": "d", "instruction": "i"}.get(ca.get("type", ""), ""))
				sh = ca.get("shared", 0)
				out.append("  %s %s × %d (%d %s each)" % (lbl, cache_size(ca["size"]), ca["count"], sh, "thread" if sh == 1 else "threads"))
		out += ["", thead(view, G)]
		u, mhz, lcpu = cp.get("cpu", NAN), cp.get("cpu_mhz", NAN), ll.get("cpu", NAN)
		u_str = "%.0f%%" % min(100.0, max(0.0, u)) if u == u else "-"
		l_str = "%.1f cores" % lcpu if lcpu == lcpu else "-"
		m_str = "%.1f GHz" % (mhz / 1000.0) if mhz == mhz else "-"
		out += block(c, view, snap, "cpu", G, gh, "CPU in use", u_str, num, "%", 0.0, 100.0, C.bcyan, True)
		out += block(c, view, snap, "llama_cpu", G, gh, "llama-server", l_str, lambda x: "%.1f" % x, " cores", 0.0, 1.0, C.bgreen, True)
		out += block(c, view, snap, "cpu_mhz", G, gh, "clock", m_str, lambda x: "%.1f" % (x / 1000.0), " GHz", None, 500.0, C.byellow)
	if not rm:
		out += ["", "%s no memory telemetry%s" % (C.grey, C.reset)]
		return paged(view, out, 0, None, H) if len(out) > 2 else out
	used = rm["total"] - rm["avail"]
	procs = max(0.0, min(rm["ws_total"], used))
	other = max(0.0, procs - ll.get("ws", 0.0))
	kernel = max(0.0, used - procs)
	out += [section("SYSTEM RAM", W, "%s total · in use %s (%.0f%%) · file cache %s · free %s · modified %s" % (gib(rm["total"]), gib(used), 100 * used / rm["total"], gib(rm["standby"]), gib(rm["free"]), gib(rm["modified"])))]
	out.append("  " + stack([(ll["ws"], C.bcyan), (other, C.blue), (kernel, C.magenta), (rm["standby"], C.yellow)], rm["total"], bw))
	out.append("  %s■%s llama-server %s    %s■%s other processes %s    %s■%s not in a working set %s    %s■%s file cache %s    %s░%s free %s" % (
		C.bcyan, C.reset, gib(ll["ws"]), C.blue, C.reset, gib(other), C.magenta, C.reset, gib(kernel), C.yellow, C.reset, gib(rm["standby"]), C.grey, C.reset, gib(rm["free"])))
	top = sorted(rm["ws_by"].items(), key=lambda kv: -kv[1][0])[:6]
	if top:
		out.append("%s  largest  %s%s" % (C.grey, " · ".join("%s%s %s" % (exe, " ×%d" % n if n > 1 else "", gib(ws)) for exe, (ws, n) in top), C.reset))
	out += ["", thead(view, G)]
	now = {"ram_used": used, "standby": rm["standby"], "free": rm["free"]}
	for label, key, col in (("RAM in use", "ram_used", C.bcyan), ("file cache", "standby", C.yellow), ("free", "free", C.bgreen)):
		out += block(c, view, snap, key, G, gh, label, gib(now[key]), gib, "", None, 2048.0, col)
	if ll["present"]:
		out += [section("LLAMA-SERVER", W, "pid %s" % ", ".join(str(p) for p in ll["pids"])), "", thead(view, G)]
		un = pm_unavailable(ll)
		out += block(c, view, snap, "llama_ws", G, gh, "working set", un or gib(ll["ws"]), gib, "", None, 1024.0, C.bcyan, False, "" if un else "private %s · commit %s" % (gib(ll["pws"]), gib(ll["commit"])))
		out += block(c, view, snap, "heap", G, gh, "private commit", gib(ll["vm"]["private"]) if ll["vm"] else "-", gib, "", None, 1024.0, C.byellow)
	cfg = {path.lower(): (lo, hi) for path, lo, hi in rm["pf_cfg"]}
	out += [section("PAGE FILE", W, "commit %s of a %s limit; %s if the page file reaches its configured maximum" % (gib(rm["commit"]), gib(rm["commit_limit"]), gib(rm["commit_max"])))]
	for p in rm["pagefiles"]:
		lo, hi = cfg.get(p["name"].lower(), (0, 0))
		out.append(kvline(p["name"], "size %s · in use %s (%.1f%%) · peak %s · %s" % (gib(p["size"]), mib(p["used"]), 100 * p["used"] / p["size"] if p["size"] else 0, mib(p["peak"]),
			"configured %s to %s" % (gib(lo), gib(hi)) if hi else "size managed by Windows")))
	if not rm["pagefiles"]:
		out.append("  %sno page file reported%s" % (C.grey, C.reset))
	out += ["", thead(view, G)]
	now = {"pf_used": sum(p["used"] for p in rm["pagefiles"]), "pages_in": rm["pages_in"], "pages_out": rm["pages_out"]}
	for label, key, span, col, fmt, unit in (("page file in use", "pf_used", 256.0, C.byellow, num, " MiB"), ("disk page-ins per s", "pages_in", 100.0, C.bred, num, ""), ("page-outs per s", "pages_out", 100.0, C.bred, num, "")):
		out += block(c, view, snap, key, G, gh, label, fmt(now[key]) + unit, fmt, unit, 0.0, span, col, True)
	return paged(view, out, 0, None, H)


# ---- page 4: requests ----------------------------------------------------------------------------------------------------

def preset_summary(c, preset, ps=None):
	rs = [r for r in c.requests if r["preset"] == preset]
	if ps is None:
		ps = prompts.group(rs, c.cfg.get("prompt_gap_s", S.CFG["prompt_gap_s"]))
	else:
		ps = [p for p in ps if p["preset"] == preset]
	# Speed figures use the long cutoffs (1,000+ new prompt tokens, 50+ generated) while at least one request reaches them,
	# and the short cutoffs (32+, 20+) otherwise, so machines that only send short chat requests still get numbers.
	pp_min = 1000 if any(r.get("pn", 0) >= 1000 and "pp" in r for r in rs) else 32
	tg_min = 50 if any(r.get("tn", 0) >= 50 and "tg" in r for r in rs) else 20
	pps = [r["pp"] for r in rs if r.get("pn", 0) >= pp_min and "pp" in r and r["pp"] == r["pp"]]
	tgs = [r["tg"] for r in rs if r.get("tn", 0) >= tg_min and "tg" in r and r["tg"] == r["tg"]]
	acc = [r["acc"] for r in rs if r.get("acc") is not None]
	mlen = [r["mlen"] for r in rs if r.get("mlen") is not None]
	new, cached = sum(r.get("pn", 0) for r in rs), sum(r.get("cached") or 0 for r in rs)
	asm = c.follow.get(preset, (None, None))[1]
	loads = [s["load_s"] for s in (asm.sessions if asm else []) if s["load_s"]]
	walls = [r["wall"] for r in rs if r.get("wall")]
	return {"prompts": len(ps), "n": len(rs), "busy": sum(r.get("ms", 0) for r in rs) / 1e3, "new": new, "cached": cached, "gen": sum(r.get("tn", 0) for r in rs), "pps": pps, "tgs": tgs,
		"acc": sum(acc) / len(acc) if acc else NAN, "mlen": sum(mlen) / len(mlen) if mlen else NAN, "hit": cached / max(1, new + cached),
		"miss": sum(1 for r in rs if r.get("sel") == "LRU" and r.get("evicted", 0) > 0), "trunc": sum(1 for r in rs if r.get("trunc")),
		"loads": len(loads), "load_s": sum(loads) / len(loads) if loads else NAN, "first": min(walls) if walls else None, "last": max(walls) if walls else None,
		"pp_min": pp_min, "tg_min": tg_min}


def requests(c, snap, view, W, H):
	presets = sorted({r["preset"] for r in c.requests})
	sel = presets[view.rsel - 1] if view.rsel and presets else None
	shown = [p for p in presets if sel is None or p == sel]
	all_ps, _ = prompt_groups(c, view, snap)
	ps = [p for p in all_ps if sel is None or p["preset"] == sel]
	rs = [r for r in c.requests if sel is None or r["preset"] == sel]
	head = [section("REQUESTS", W, "%d prompts · %d turns · %s   (m: preset  t: turns)" % (len(ps), len(rs), sel or "all presets"))]
	if not rs:
		empty = "no finished requests in %s yet" % c.cfg["log_dir"] if c.cfg.get("log_dir") else "no finished requests yet"
		return head + ["", "  %s%s%s" % (C.grey, empty, C.reset)]
	sums = {p: preset_summary(c, p, all_ps) for p in shown}
	st = bucket_stats(c, view, snap)
	issues_by_p = {}
	for p in shown:
		s = sums[p]
		iss = []
		if s["miss"]:
			iss.append("%d cache reset%s" % (s["miss"], "" if s["miss"] == 1 else "s"))
		if s["trunc"]:
			iss.append("%d cut off" % s["trunc"])
		issues_by_p[p] = " · ".join(iss) if iss else "-"
	iss_w = max(8, max(len(v) for v in issues_by_p.values()) + 2)
	head += ["", section("volume", W),
		C.grey + "  " + pad("preset", 24) + rpad("prompts", 8) + rpad("turns", 6) + rpad("busy", 8) + rpad("input", 10) + rpad("new", 10) + rpad("cached", 10) + rpad("output", 9) + rpad("cache hit", 10) + rpad("loads", 12) + rpad("issues", iss_w) + "   active" + C.reset]
	for p in shown:
		s = sums[p]
		loads_str = "%d (%.1f s)" % (s["loads"], s["load_s"]) if s["loads"] and s["load_s"] == s["load_s"] else str(s["loads"]) if s["loads"] else "-"
		head.append("  " + pad(p, 24) + rpad(str(s["prompts"]), 8) + rpad(str(s["n"]), 6) + rpad(mmss(s["busy"]), 8) + rpad(tok(s["new"] + s["cached"]), 10) + rpad(tok(s["new"]), 10) + rpad(tok(s["cached"]), 10) + rpad(tok(s["gen"]), 9) + rpad("%.0f%%" % (100 * s["hit"]), 10)
			+ rpad(loads_str, 12) + rpad(issues_by_p[p], iss_w) + "   %s – %s" % (clock(s["first"]), clock(s["last"])))
	cuts = {(s["pp_min"], s["tg_min"]) for s in sums.values() if s["n"]}
	differ = len(cuts) != 1
	if differ:  # presets that never reach the long cutoffs fall back to the short cutoffs, per preset
		sub = "prefill over %s+ new tokens, decode over %s+ (per preset)" % (tok(min(m for m, _ in cuts)), tok(min(t for _, t in cuts)))
	else:
		pp_min, tg_min = next(iter(cuts))
		sub = "prefill over %s+ new tokens, decode over %s+" % (tok(pp_min), tok(tg_min))
	head += ["", section("speed, t/s", W, sub),
		C.grey + "  " + pad("preset", 24) + rpad("prefill median", 15) + rpad("slow 10%", 10) + rpad("n", 6) + rpad("decode median", 15) + rpad("slow 10%", 10) + rpad("n", 6) + rpad("draft acc", 11) + rpad("mean len", 10) + C.reset]
	for p in shown:
		s = sums[p]
		if differ:  # the label must not widen the preset column: keep the cell 24 wide, shortening the name from the start
			label = "%s %s+/%s+" % (p, tok(s["pp_min"]), tok(s["tg_min"]))
			name = label if len(label) <= 24 else "..." + label[-21:]
		else:
			name = p
		head.append("  " + pad(name, 24) + rpad(f0(S.median(s["pps"])), 15) + rpad(f0(pct(s["pps"], 0.1)), 10) + rpad(str(len(s["pps"])), 6) + rpad(f0(S.median(s["tgs"]), "%.1f"), 15) + rpad(f0(pct(s["tgs"], 0.1), "%.1f"), 10)
			+ rpad(str(len(s["tgs"])), 6) + rpad(f0(100 * s["acc"], "%.0f%%"), 11) + rpad(f0(s["mlen"], "%.2f"), 10))
	for title, sub_b, table, unit in (("prefill t/s by context", "median (turns); green fastest · red slowest in each column", st[0], "pp"), ("decode t/s by context", "median (turns); green fastest · red slowest in each column", st[1], "tg")):
		head += ["", section(title, W, sub_b), C.grey + "  " + pad("preset", 24) + "".join(rpad(lb, 13) for lb in DEC_LABELS) + C.reset]
		cols = []  # ranked colours per column, over every preset so a filter does not change them
		for b in range(len(DEC_LABELS)):
			values = [(S.median(table[(q, b)]) if table.get((q, b)) else NAN, len(table.get((q, b), []))) for q in presets]
			cols.append(rank_colours(values))
		for p in shown:
			i = presets.index(p)
			cells = []
			for b in range(len(DEC_LABELS)):
				xs = table.get((p, b), [])
				med = S.median(xs) if xs else NAN
				text = "%s (%d)" % (f0(med, "%.0f" if unit == "pp" else "%.1f"), len(xs)) if xs else "-"
				cells.append(cols[b][i] + rpad(text, 13) + C.reset)
			head.append("  " + pad(p, 24) + "".join(cells))
	w, gh = max(20, min(120, W - 80)), view.gh or 2
	pts = w * 2 if STYLE["mode"] == "braille" else w
	pp = [r["pp"] for r in rs if r.get("pn", 0) >= 256 and "pp" in r and r["pp"] == r["pp"]][-pts:]
	tg = [r["tg"] for r in rs if r.get("tn", 0) >= 20 and "tg" in r and r["tg"] == r["tg"]][-pts:]
	tail = rs[-pts:]
	sp_ = [spill_of(c, r) for r in tail if spill_of(c, r) == spill_of(c, r)]
	show_sp = bool(c.allow_known and any(v - c.allow >= c.cfg["spill_mib"] for v in sp_))
	head += ["", section("per turn", W, "last %d turns with 256+ new tokens, %d with 20+ decode" % (len(pp), len(tg))), ""]
	charts = [("prefill t/s", pp or [NAN], C.bcyan, 100.0, ""), ("decode t/s", tg or [NAN], C.bgreen, 20.0, "")]
	if show_sp:
		charts.append(("shared GPU memory", sp_, C.bred, 512.0, " MiB"))
	for label, vals, col, span, unit in charts:
		head += block(c, view, snap, None, 0, gh, label, "", num, unit, 0.0, span, col, True, vals=vals)
	sub_ep = "newest first · turns shown (t: hide)" if getattr(view, "turns", False) else "newest first (t: show turns)"
	pw, drop, notes = prompt_layout(W, [p["preset"] for p in ps], sel is None)
	head += ["", section("every prompt", W, sub_ep), legend(), req_header(sel is None, pw=pw, drop=drop, notes=notes)]
	items = []
	for p in reversed(ps):
		items.append(("p", p, ""))
		if getattr(view, "turns", False):
			n_t = len(p["turns"])
			for j, r in enumerate(p["turns"]):
				items.append(("t", r, "  └ " if j == n_t - 1 else "  · "))
	return paged(view, head, len(items), lambda i: prompt_row(c, items[i][1], st, sel is None, pw=pw, drop=drop) if items[i][0] == "p" else turn_row(c, items[i][1], st, sel is None, items[i][2], True, pw=pw, drop=drop, nested=True), H, pin=req_header(sel is None, pw=pw, drop=drop, notes=notes))


# ---- page 5: events --------------------------------------------------------------------------------------------------------

LEVEL_FILTER = (("all", 0), ("warnings and errors", 1), ("errors only", 2))


def events(c, snap, view, W, H):
	label, lvl = LEVEL_FILTER[view.level % 3]
	evs = [e for e in c.events if S.LEVELS[e["level"]] >= lvl][::-1]
	head = [section("EVENTS", W, "%d shown · %s   (f: filter, d: detail)" % (len(evs), label)), ""]
	head += notice_lines(snap, None)  # every notice, untruncated
	if not evs:
		return head + ["  %snothing yet%s" % (C.grey, C.reset)]

	dw = max(20, W - 9)  # wrap width for the detail continuation lines
	lines = list(head)
	for e in evs:
		icon, col = ICON[e["level"]]
		lines.append(" %s  %s%s%s  %s  %s" % (pad(clock(e["wall"], True), 11), color(col), icon, C.reset, pad(e["kind"], 7), e["msg"]))
		if view.detail and e.get("detail"):
			lines += ["    %s%s%s" % (C.grey, w, C.reset) for w in textwrap.wrap(e["detail"], dw)]
	# scrolled by line, so a detail line and its headline stay together in the visible window
	view.scroll = max(0, min(view.scroll, max(0, len(lines) - H)))
	return lines[view.scroll:view.scroll + H]


# ---- page 6: tensors ---------------------------------------------------------------------------------------------------------

TAG_HELP = (("-ot rule", "your --override-tensor rule"), ("--cpu-moe", "your --cpu-moe flag"), ("--n-cpu-moe", "your --n-cpu-moe flag: experts of the first layers"),
	("--n-gpu-layers", "outside the layers you offload with --n-gpu-layers"), ("llama.cpp default", "llama.cpp keeps input embeddings in system RAM without a flag (per llama-fit-params)"),
	("tied copy", "the output head reuses token_embd, and llama-fit-params counts a second copy on the GPU"))


def kind_label(kind):
	return {"full": "full attention", "swa": "sliding window", "linear": "linear / SSM", "mtp": "MTP head", "other": ""}.get(kind, "")


def badge(dev):
	col = C.bgreen if dev == "GPU" else C.byellow if dev == "CPU" else C.magenta
	return col + "■ " + dev + C.reset


def tag(text):
	return "%s[%s]%s" % (C.byellow, text, C.reset) if text else ""


def tensors(c, snap, view, W, H):
	name, order, loaded = pick_model(c, snap, view)
	if name is None:
		return ["", "%s %s%s" % (C.grey, no_models(c), C.reset)]
	entry = c.mem.get(name, {})
	mode = "tensors" if view.tmode else "layers"
	dev = DEVICES[view.dev % 3]
	head = [section("TENSORS", W, "%s · %s   (m: model, t: layers/tensors, d: device)" % (name, "loaded" if name in loaded else "not loaded, as llama.cpp will place them"))]
	if entry.get("state") != "ready":
		return head + ["", "  %s%s%s" % (C.byellow, "analysing the model…" if entry.get("state") != "error" else "could not analyse: " + entry["error"], C.reset)]
	m = entry["map"]
	roster = [t for t in m["roster"] if dev == "all" or t["dev"] == dev]
	gpu = [t for t in m["roster"] if t["dev"] == "GPU"]
	cpu = [t for t in m["roster"] if t["dev"] == "CPU"]
	head.append("  %d tensors in %d blocks     %s %s (%d)     %s %s (%d)      %sview%s %s   %sdevice%s %s" % (
		len(m["roster"]), m["n_layer"], badge("GPU"), mib(sum(t["mib"] for t in gpu)), len(gpu), badge("CPU"), mib(sum(t["mib"] for t in cpu)), len(cpu), C.grey, C.reset, C.bold + mode + C.reset, C.grey, C.reset, C.bold + dev + C.reset))
	for tg in sorted({t["why"] for t in m["roster"] if t["why"]}):
		more = "  " + C.grey + ", ".join(m["rules"]) + C.reset if tg == "-ot rule" and m["rules"] else ""
		head.append("  %s  %s%s%s%s" % (tag(tg), C.grey, next((h for p, h in TAG_HELP if tg.startswith(p)), ""), C.reset, more))
	head.append("")
	if mode == "tensors":
		nw = max(24, min(48, W - 64))
		pin = "%s  %s%s%s%s  %s%s" % (C.grey, pad("tensor", nw), pad("type", 9), pad("shape", 18), rpad("MiB", 9), pad("device", 9), "tag" + C.reset)

		def row(i):
			t = roster[i]
			return "  %s%s%s%s  %s%s" % (pad(t["name"], nw), pad(t["type"], 9), pad(" × ".join(str(d) for d in t["dims"]), 18), rpad("%.1f" % t["mib"], 9), pad(badge(t["dev"]), 9), tag(t["why"]))

		head.append(pin)
		return paged(view, head, len(roster), row, H, pin=pin)
	groups = {}
	for t in roster:
		key = ("blk", t["layer"]) if t["layer"] is not None else ("model", t["comp"])
		groups.setdefault(key, []).append(t)
	keys = sorted(groups, key=lambda k: (0 if k[0] == "model" and k[1] == "embeddings" else 1 if k[0] == "blk" else 2, k[1] if k[0] == "blk" else 0, str(k[1])))
	top = max((sum(t["mib"] for t in v) for v in groups.values()), default=1.0)
	pin = "%s  %s%s%s%s%s%s%s" % (C.grey, pad("block", 18), pad("kind", 16), rpad("MiB", 9), pad("  size", 24), pad("tensors", 9), pad("device", 9), "tag" + C.reset)
	head.append(pin)

	def lrow(i):
		k = keys[i]
		ts = groups[k]
		size, devs = sum(t["mib"] for t in ts), {t["dev"] for t in ts}
		label = ("blk %d" % k[1]) if k[0] == "blk" else k[1]
		kind = kind_label(ts[0]["kind"]) if k[0] == "blk" else ""
		d = "GPU" if devs == {"GPU"} else "CPU" if devs == {"CPU"} else "mixed"
		col = C.bgreen if d == "GPU" else C.byellow if d == "CPU" else C.magenta
		return "  %s%s%s  %s  %s%s%s" % (pad(label, 18), pad(kind, 16), rpad("%.1f" % size, 9), bar(size / top, 20, col), pad(str(len(ts)), 9), pad(badge(d), 9), " ".join(tag(w) for w in sorted({t["why"] for t in ts if t["why"]})))

	return paged(view, head, len(keys), lrow, H, pin=pin)
