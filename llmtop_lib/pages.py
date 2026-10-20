# SPDX-License-Identifier: GPL-3.0-or-later
"""The six llmtop pages (live, memory, system, requests, events, tensors) and the frame that wraps them."""

import os
import textwrap
import time
from collections import defaultdict

from . import model
from . import state as S
from .state import mib
from .ui import STYLE, C, ago, bar, clip, gib, num, pad, plot, rate, rpad, shade, stack, tok, vlen

NAN = float("nan")
ICON = {"crit": ("◆", "bred"), "warn": ("▲", "byellow"), "info": ("●", "bcyan")}
PRE_EDGES, PRE_LABELS = (256, 1000, 4000, 16000, 64000), ("<256", "256-1k", "1-4k", "4-16k", "16-64k", "64k+")
DEC_EDGES, DEC_LABELS = (8000, 32000, 64000), ("<8k", "8-32k", "32-64k", "64k+")
DEVICES = ("all", "GPU", "CPU")


class View:
	PAGES = ("Live", "Memory", "System", "Requests", "Events", "Tensors")
	WINDOWS = ((60, "1m"), (300, "5m"), (900, "15m"), (3600, "1h"), (21600, "6h"), (86400, "24h"))

	def __init__(self, window=1):
		self.page, self.win, self.scroll, self.msel, self.rsel, self.level, self.paused, self.msg = 0, window, 0, 0, 0, 0, False, ""
		self.gh, self.tmode, self.dev = 0, 0, 0  # graph height (0 = fit the terminal), tensor view mode, tensor device filter
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
	n = len(gr)
	if n == 1:
		axis = "%s – %s%s" % (fmt(lo), fmt(hi), unit) if fin else "no data yet"
		return ["  " + pad(label, 24) + rpad(now, 14) + "  " + g + "│" + r + pad(gr[0], G) + g + "│" + r + "  " + g + axis + r + ("   " + note if note else "")]
	ticks = {G * k // 4 for k in (1, 2, 3)}
	top = "┌" + "".join("┬" if i in ticks else "─" for i in range(G)) + "┐"
	bottom = "└" + "".join("┴" if i in ticks else "─" for i in range(G)) + "┘"
	lead = "  " + " " * 38 + "  "
	out = [lead + g + top + r + "  " + g + ((fmt(hi) + unit) if fin else "no data yet") + r]
	for i, line in enumerate(gr):
		left = pad(label, 24) + rpad(now, 14) if i == 0 else " " * 38
		out.append("  " + left + "  " + g + "│" + r + pad(line, G) + g + "│" + r + ("   " + note if note and i == n // 2 else ""))
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


def bucket_stats(c, view, snap):
	"""Median throughput of similar requests: prefill by new prompt tokens, decode by context depth, per preset (cached per second)."""
	sec = int(snap["t"])
	if view.cache.get("bk_sec") != sec:
		pre, dec = defaultdict(list), defaultdict(list)
		for r in c.requests:
			if "pp" in r and r.get("pn", 0) >= 32:
				pre[(r["preset"], bucket(r["pn"], PRE_EDGES))].append(r["pp"])
			if "tg" in r and r.get("tn", 0) >= 20:
				dec[(r["preset"], bucket(r.get("kv", 0), DEC_EDGES))].append(r["tg"])
		view.cache["bk"], view.cache["bk_sec"] = (pre, dec), sec
	return view.cache["bk"]


def ref(xs):
	return S.median(xs) if len(xs) >= 3 else NAN


def rel(x, reference):
	"""Colour a throughput against the usual value for similar requests."""
	if x != x:
		return C.grey
	if reference != reference or reference <= 0:
		return ""
	return C.bgreen if x >= 0.8 * reference else C.byellow if x >= 0.5 * reference else C.bred


def legend():
	return "%s  colour: t/s against what is typical for this preset at a similar size   %s■%s 80%% or more   %s■%s 50-80%%   %s■%s under 50%%%s" % (C.grey, C.bgreen, C.grey, C.byellow, C.grey, C.bred, C.grey, C.reset)


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
		words = textwrap.wrap(a["msg"], max(30, W - 18))
		out.append(prefix + words[0])
		out += [" " * 17 + w for w in words[1:3]]
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
	keys += {"Live": "", "Memory": "   m next model", "System": "", "Requests": "   m preset filter", "Events": "   f level filter", "Tensors": "   t layers/tensors   d device   m next model"}[page]
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
		out += block(c, view, snap, "spill", G, gh, "shared (system RAM)", "%s%s MiB%s" % (C.bred + C.bold if moving else C.byellow + C.bold if grown else "", num(ll["shr"]), C.reset), num, " MiB", None, 512.0, C.bred if moving else C.byellow, False,
			" · ".join(x for x in ("%s MiB when resident, %+d since" % (num(ll["floor"]), round(ll["shr"] - ll["floor"])) if ll["floor_known"] else "", ("CPU-side buffers %s MiB, %s" % (num(ll["allow"]), "measured with no VRAM pressure" if ll["allow_src"] == "measured" else "projected by llama.cpp")) if ll["allow_known"] else "",
			"llama-server holds %.0f%% of all shared" % (100 * ll["shr"] / g["shr"]) if g["shr"] > 0 else "") if x))
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
	pi = rm["pages_in"]
	out += block(c, view, snap, "pages_in", G, gh, "disk page-ins per s", "%s%.0f%s" % (C.bred if pi >= c.cfg["pagein_warn"] else "", pi, C.reset), num, "", 0.0, 100.0, C.byellow, True, "page-outs %.0f/s" % rm["pages_out"])
	return out


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


def notes_legend():
	return "%s  notes: LCP n%% = share of the slot's prompt prefix reused · LRU n = no similar prefix, oldest slot reused (it held n tokens) · shared n = llama-server shared GPU memory in MiB%s" % (C.grey, C.reset)


REQ_COLS = (("time", 11, "l"), ("task", 5, "r"), ("preset", 24, "l"), ("new tok", 8, "r"), ("cached", 8, "r"), ("prefill", 11, "r"), ("gen", 7, "r"), ("decode", 10, "r"), ("draft", 5, "r"), ("total", 6, "r"))


def req_line(cells, with_preset, tail=""):
	parts = []
	for (_, width, align), cell in zip(REQ_COLS, cells, strict=True):
		if not with_preset and width == 24:
			continue
		parts.append(pad(cell, width) if align == "l" else rpad(cell, width))
	return "  " + "  ".join(parts) + "  " + tail


def req_header(with_preset):
	return C.grey + req_line([name for name, _, _ in REQ_COLS], with_preset, "notes") + C.reset


def req_row(c, r, stats, with_preset):
	pre, dec = stats
	pp, tg = r.get("pp", NAN), r.get("tg", NAN)
	pp_ref = ref(pre.get((r["preset"], bucket(r.get("pn", 0), PRE_EDGES)), [])) if r.get("pn", 0) >= 32 else NAN
	tg_ref = ref(dec.get((r["preset"], bucket(r.get("kv", 0), DEC_EDGES)), [])) if r.get("tn", 0) >= 20 else NAN
	notes = []
	if r.get("sel") == "LRU" and r.get("evicted", 0) > 0:
		notes.append("%sLRU %s%s" % (C.byellow, tok(r["evicted"]), C.reset))
	elif r.get("sel") == "LCP":
		notes.append("%sLCP %.0f%%%s" % (C.grey, 100 * r["sim"], C.reset))
	sp = spill_of(c, r)
	if c.allow_known and sp == sp and sp - c.allow >= c.cfg["spill_mib"]:
		notes.append("%sshared %s%s" % (C.byellow, num(sp), C.reset))
	if r.get("trunc"):
		notes.append(C.bred + "truncated" + C.reset)
	if r.get("reallocs"):
		notes.append(C.byellow + "buffers resized" + C.reset)
	cells = [clock(r.get("wall")), str(r["task"]), r["preset"][:24], tok(r.get("pn", 0)), "-" if r.get("cached", 0) is None else tok(r.get("cached", 0)),
		"%s%s t/s%s" % (rel(pp, pp_ref), "-" if pp != pp else "%.0f" % pp, C.reset), tok(r.get("tn", 0)),
		"%s%s t/s%s" % (rel(tg, tg_ref), "-" if tg != tg else "%.1f" % tg, C.reset),
		"%.0f%%" % (100 * r["acc"]) if r.get("acc") is not None else "-", mmss(r.get("ms", 0) / 1e3)]
	return req_line(cells, with_preset, " ".join(notes))


def recent_requests(c, snap, view, W, n, preset=None):
	rs = [r for r in c.requests if preset is None or r["preset"] == preset]
	if not rs or n < 5:
		return []
	st = bucket_stats(c, view, snap)
	return [section("recent requests", W), legend(), notes_legend(), req_header(preset is None)] + [req_row(c, r, st, preset is None) for r in reversed(rs[-(n - 4):])]


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
		out = gpu_block(c, snap, view, W, gh) + [""] + ram_block(c, snap, view, W, gh)
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
	reqs = recent_requests(c, snap, view, W, req_n, active[0][0] if len(active) == 1 else None)
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
			if ll["floor_known"]:
				bits.append("%s when the model became resident (lowest since), %+d since" % (mib(ll["floor"]), round(growth)))
			if ll["allow_known"]:
				bits.append("CPU-side buffers %s (%s)" % (mib(ll["allow"]), "measured on an earlier load with resident + shared at least %s under the budget" % mib(c.cfg["idle_headroom_mib"]) if ll["allow_src"] == "measured" else "projected by llama.cpp, not verified for this preset"))
			if g["shr"] > 0:
				bits.append("llama-server holds %.0f%% of all shared GPU memory" % (100 * ll["shr"] / g["shr"]))
			out.append("  " + "  ·  ".join(bits))
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

def system(c, snap, view, W, H):
	rm, ll = snap["ram"], snap["llama"]
	if not rm:
		return ["", "%s no memory telemetry%s" % (C.grey, C.reset)]
	G, gh, bw = graph_w(W), view.gh or 2, max(30, min(80, W - 40))
	used = rm["total"] - rm["avail"]
	procs = max(0.0, min(rm["ws_total"], used))
	other = max(0.0, procs - ll["ws"])
	kernel = max(0.0, used - procs)
	out = [section("SYSTEM RAM", W, "%s total · in use %s (%.0f%%) · file cache %s · free %s · modified %s" % (gib(rm["total"]), gib(used), 100 * used / rm["total"], gib(rm["standby"]), gib(rm["free"]), gib(rm["modified"])))]
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

def preset_summary(c, preset):
	rs = [r for r in c.requests if r["preset"] == preset]
	# Speed figures use the long cutoffs (1,000+ new prompt tokens, 50+ generated) while at least one request reaches them,
	# and the bucket cutoffs (32+, 20+) otherwise, so machines that only send short chat requests still get numbers.
	pp_min = 1000 if any(r.get("pn", 0) >= 1000 and "pp" in r for r in rs) else 32
	tg_min = 50 if any(r.get("tn", 0) >= 50 and "tg" in r for r in rs) else 20
	pps = [r["pp"] for r in rs if r.get("pn", 0) >= pp_min and "pp" in r]
	tgs = [r["tg"] for r in rs if r.get("tn", 0) >= tg_min and "tg" in r]
	acc = [r["acc"] for r in rs if r.get("acc") is not None]
	mlen = [r["mlen"] for r in rs if r.get("mlen") is not None]
	new, cached = sum(r.get("pn", 0) for r in rs), sum(r.get("cached") or 0 for r in rs)
	asm = c.follow.get(preset, (None, None))[1]
	loads = [s["load_s"] for s in (asm.sessions if asm else []) if s["load_s"]]
	walls = [r["wall"] for r in rs if r.get("wall")]
	return {"n": len(rs), "busy": sum(r.get("ms", 0) for r in rs) / 1e3, "new": new, "cached": cached, "gen": sum(r.get("tn", 0) for r in rs), "pps": pps, "tgs": tgs,
		"acc": sum(acc) / len(acc) if acc else NAN, "mlen": sum(mlen) / len(mlen) if mlen else NAN, "hit": cached / max(1, new + cached),
		"miss": sum(1 for r in rs if r.get("sel") == "LRU" and r.get("evicted", 0) > 0), "trunc": sum(1 for r in rs if r.get("trunc")),
		"loads": len(loads), "load_s": sum(loads) / len(loads) if loads else NAN, "first": min(walls) if walls else None, "last": max(walls) if walls else None,
		"pp_min": pp_min, "tg_min": tg_min}


def rival_of(c, r):
	"""Highest GPU load (%) of any other process while the request ran (NaN if llmtop had no data then)."""
	if "rival" not in r:
		r["rival"] = c.rings["gpu_other"].span_max(r["wall0"], r["wall"]) if r.get("wall0") and r.get("wall") else NAN
	return r["rival"]


def effect_lines(c, shown):
	"""Decode speed at the same context depth with and without two conditions that were recorded during each request. A correlation in the data, not proof of cause."""
	cfg, rows = c.cfg, []
	for p in shown:
		for b in range(len(DEC_LABELS)):
			rs = [r for r in c.requests if r["preset"] == p and "tg" in r and r.get("tn", 0) >= 20 and bucket(r.get("kv", 0), DEC_EDGES) == b]
			groups, usable = [], False
			for kind, thr in (("shared", cfg["spill_mib"]), ("rival", cfg["rival_gpu_pct"])):
				hi, lo = [], []
				for r in rs:
					v = NAN if kind == "shared" and not c.allow_known else (spill_of(c, r) - c.allow if kind == "shared" else rival_of(c, r))
					if v == v:
						(hi if v >= thr else lo).append(r["tg"])
				groups.append((hi, lo))
				usable = usable or (len(hi) >= 3 and len(lo) >= 3)
			if usable:
				rows.append((p, DEC_LABELS[b], groups))
	if not rows:
		return ["  %snot enough requests yet: needs 3 or more with and 3 or more without a condition at the same context depth%s" % (C.grey, C.reset)]
	out = [C.grey + "  " + pad("preset", 24) + pad("depth", 8) + rpad("shared above", 15) + rpad("shared below", 15) + rpad("ratio", 8) + rpad("apps above", 15) + rpad("apps below", 15) + rpad("ratio", 8) + C.reset]
	for p, label, groups in rows:
		cells = []
		for hi, lo in groups:
			ok = len(hi) >= 3 and len(lo) >= 3
			mh, ml = (S.median(hi) if len(hi) >= 3 else NAN), (S.median(lo) if len(lo) >= 3 else NAN)
			cells += [rpad("%s (%d)" % (f0(mh, "%.1f"), len(hi)) if len(hi) >= 3 else "-", 15), rpad("%s (%d)" % (f0(ml, "%.1f"), len(lo)) if len(lo) >= 3 else "-", 15),
				(rel(mh, ml) if ok else C.grey) + rpad("%.2f×" % (mh / ml) if ok and ml else "-", 8) + C.reset]
		out.append("  " + pad(p, 24) + pad(label, 8) + "".join(cells))
	return out


def requests(c, snap, view, W, H):
	presets = sorted({r["preset"] for r in c.requests})
	sel = presets[view.rsel - 1] if view.rsel and presets else None
	shown = [p for p in presets if sel is None or p == sel]
	rs = [r for r in c.requests if sel is None or r["preset"] == sel]
	head = [section("REQUESTS", W, "%d recorded · %s   (m: cycle presets)" % (len(rs), sel or "all presets"))]
	if not rs:
		empty = "no finished requests in %s yet" % c.cfg["log_dir"] if c.cfg.get("log_dir") else "no finished requests yet"
		return head + ["", "  %s%s%s" % (C.grey, empty, C.reset)]
	sums = {p: preset_summary(c, p) for p in shown}
	st = bucket_stats(c, view, snap)
	head += ["", section("volume", W),
		C.grey + "  " + pad("preset", 24) + rpad("reqs", 5) + rpad("busy", 9) + rpad("prompt tokens", 15) + rpad("new", 11) + rpad("cached", 11) + rpad("generated", 11) + rpad("cache hit", 10) + rpad("LRU picks", 10) + rpad("cut off", 8) + rpad("loads", 7) + rpad("load s", 8) + "   active" + C.reset]
	for p in shown:
		s = sums[p]
		head.append("  " + pad(p, 24) + rpad(str(s["n"]), 5) + rpad(mmss(s["busy"]), 9) + rpad(tok(s["new"] + s["cached"]), 15) + rpad(tok(s["new"]), 11) + rpad(tok(s["cached"]), 11) + rpad(tok(s["gen"]), 11) + rpad("%.0f%%" % (100 * s["hit"]), 10)
			+ rpad(str(s["miss"]), 10) + rpad(str(s["trunc"]), 8) + rpad(str(s["loads"]), 7) + rpad(f0(s["load_s"], "%.1f"), 8) + "   %s – %s" % (clock(s["first"]), clock(s["last"])))
	cuts = {(s["pp_min"], s["tg_min"]) for s in sums.values() if s["n"]}
	differ = len(cuts) != 1
	if differ:  # presets that never reach the long cutoffs fall back to the bucket cutoffs, per preset
		sub = "prefill over prompts of %s+ new tokens, decode over %s+ tokens, per preset (cutoffs in the preset column); slow 10%% = the slowest tenth" % (tok(min(m for m, _ in cuts)), tok(min(t for _, t in cuts)))
	else:
		pp_min, tg_min = next(iter(cuts))
		sub = "prefill over prompts of %s+ new tokens, decode over %s+ tokens; slow 10%% = the slowest tenth" % (tok(pp_min), tok(tg_min))
	head += ["", section("speed, t/s", W, sub),
		C.grey + "  " + pad("preset", 24) + rpad("prefill median", 15) + rpad("slow 10%", 10) + rpad("worst", 8) + rpad("decode median", 15) + rpad("slow 10%", 10) + rpad("worst", 8) + rpad("draft acc", 11) + rpad("mean len", 10) + C.reset]
	for p in shown:
		s = sums[p]
		if differ:  # the label must not widen the preset column: keep the cell 24 wide, shortening the name from the start
			label = "%s %s+/%s+" % (p, tok(s["pp_min"]), tok(s["tg_min"]))
			name = label if len(label) <= 24 else "..." + label[-21:]
		else:
			name = p
		head.append("  " + pad(name, 24) + rpad(f0(S.median(s["pps"])), 15) + rpad(f0(pct(s["pps"], 0.1)), 10) + rpad(f0(min(s["pps"]) if s["pps"] else NAN), 8) + rpad(f0(S.median(s["tgs"]), "%.1f"), 15) + rpad(f0(pct(s["tgs"], 0.1), "%.1f"), 10)
			+ rpad(f0(min(s["tgs"]) if s["tgs"] else NAN, "%.1f"), 8) + rpad(f0(100 * s["acc"], "%.0f%%"), 11) + rpad(f0(s["mlen"], "%.2f"), 10))
	for title, labels, table, unit in (("prefill t/s by new prompt tokens", PRE_LABELS, st[0], "pp"), ("decode t/s by context depth", DEC_LABELS, st[1], "tg")):
		head += ["", section(title, W, "median, requests in brackets; colour is against the best preset in each column (3+ requests)"), C.grey + "  " + pad("preset", 24) + "".join(rpad(lb, 13) for lb in labels) + C.reset]
		best = []  # the fastest preset in each column, over every preset so a filter does not change the colours
		for b in range(len(labels)):
			meds = [S.median(table[(q, b)]) for q in presets if len(table.get((q, b), [])) >= 3]
			best.append(max(meds) if meds else NAN)
		for p in shown:
			cells = []
			for b in range(len(labels)):
				xs = table.get((p, b), [])
				text = "%s (%d)" % (f0(S.median(xs), "%.0f" if unit == "pp" else "%.1f"), len(xs)) if xs else "-"
				cells.append((rel(S.median(xs), best[b]) if len(xs) >= 3 else C.grey if not xs else "") + rpad(text, 13) + C.reset)
			head.append("  " + pad(p, 24) + "".join(cells))
	head += ["", section("effect of shared GPU memory and other apps", W, "decode t/s, median (requests), same context depth. above = shared memory %s MiB or more over the baseline / another process %s%% or more of the GPU during the request. A correlation, not proof of cause" % (c.cfg["spill_mib"], c.cfg["rival_gpu_pct"]))]
	head += effect_lines(c, shown)
	w, gh = max(20, min(120, W - 80)), view.gh or 2
	tail = rs[-w * 2:]
	pp_min = 500 if any(r.get("pn", 0) >= 500 and "pp" in r for r in tail) else 32
	tg_min = 50 if any(r.get("tn", 0) >= 50 and "tg" in r for r in tail) else 20
	pp = [r.get("pp", NAN) if r.get("pn", 0) >= pp_min else NAN for r in tail]
	tg = [r.get("tg", NAN) if r.get("tn", 0) >= tg_min else NAN for r in tail]
	sp_ = [spill_of(c, r) for r in tail]
	head += ["", section("per request", W, "oldest to newest, last %d" % len(tail)), ""]
	for label, vals, col, span, unit in (("prefill t/s", pp, C.bcyan, 100.0, ""), ("decode t/s", tg, C.bgreen, 20.0, ""), ("shared GPU memory", sp_, C.bred, 512.0, " MiB")):
		head += block(c, view, snap, None, 0, gh, label, "", num, unit, 0.0, span, col, True, vals=vals)
	slow = sorted((r for r in rs if r.get("pn", 0) >= 4000 and "pp" in r), key=lambda r: r["pp"])[:5]
	if slow:
		head += ["", section("slowest prefills", W, "prompts of 4,000+ new tokens"), req_header(sel is None)] + [req_row(c, r, st, sel is None) for r in slow]
	head += ["", section("every request", W, "newest first"), legend(), notes_legend(), req_header(sel is None)]
	rev = rs[::-1]
	return paged(view, head, len(rev), lambda i: req_row(c, rev[i], st, sel is None), H, pin=req_header(sel is None))


# ---- page 5: events --------------------------------------------------------------------------------------------------------

LEVEL_FILTER = (("all", 0), ("warnings and errors", 1), ("errors only", 2))


def events(c, snap, view, W, H):
	label, lvl = LEVEL_FILTER[view.level % 3]
	evs = [e for e in c.events if S.LEVELS[e["level"]] >= lvl][::-1]
	head = [section("EVENTS", W, "%d shown · %s   (f: change filter)" % (len(evs), label)), ""]
	head += notice_lines(snap, None)  # every notice, untruncated
	if not evs:
		return head + ["  %snothing yet%s" % (C.grey, C.reset)]

	def row(i):
		e = evs[i]
		icon, col = ICON[e["level"]]
		return " %s  %s%s%s  %s  %s" % (pad(clock(e["wall"], True), 11), color(col), icon, C.reset, pad(e["kind"], 7), e["msg"])

	return paged(view, head, len(evs), row, H)


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
