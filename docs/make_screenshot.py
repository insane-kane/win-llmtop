#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Aaron Kane <software@kanenet.us>
"""Generate a reproducible SVG screenshot of the win-llmtop Live page."""

import argparse
import os
import random
import re
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from llmtop_lib import pages, state as S, ui  # noqa: E402

FIXED_TIME = 1700000000.0  # 2023-11-14 22:13:20 UTC
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "img", "live.svg")

DEFAULT_BG = "#16181d"
DEFAULT_FG = "#abb2bf"

PALETTE_STANDARD = [
	"#282c34",  # 0: black
	"#e06c75",  # 1: red
	"#98c379",  # 2: green
	"#e5c07b",  # 3: yellow
	"#61afef",  # 4: blue
	"#c678dd",  # 5: magenta
	"#56b6c2",  # 6: cyan
	"#dcdfe4",  # 7: white
]

PALETTE_BRIGHT = [
	"#6c7585",  # 0: grey (90)
	"#ff6b6b",  # 1: bred (91)
	"#73daca",  # 2: bgreen (92)
	"#f0c674",  # 3: byellow (93)
	"#7aa2f7",  # 4: bblue (94)
	"#bb9af7",  # 5: bmagenta (95)
	"#7dcfff",  # 6: bcyan (96)
	"#ffffff",  # 7: bwhite (97)
]

RE_SGR = re.compile(r"\x1b\[([0-9;]*)m")


def get_256_color(idx):
	if 0 <= idx < 8:
		return PALETTE_STANDARD[idx]
	if 8 <= idx < 16:
		return PALETTE_BRIGHT[idx - 8]
	if 16 <= idx < 232:
		idx -= 16
		b = (idx % 6) * 51
		idx //= 6
		g = (idx % 6) * 51
		r = (idx % 6) * 51
		return "#%02x%02x%02x" % (r, g, b)
	if 232 <= idx < 256:
		gray = (idx - 232) * 10 + 8
		return "#%02x%02x%02x" % (gray, gray, gray)
	return DEFAULT_FG


def make_demo_state():
	time.time = lambda: FIXED_TIME
	time.localtime = lambda t=None: time.gmtime(FIXED_TIME if t is None else t)

	rng = random.Random(1337)
	t_now = int(FIXED_TIME)
	t_load = t_now - 2520

	rings = {k: S.Ring(span=7200) for k in S.SERIES}

	# Simulated request active intervals
	intervals = [
		(t_now - 2100, t_now - 2085),
		(t_now - 1600, t_now - 1580),
		(t_now - 1100, t_now - 1080),
		(t_now - 650, t_now - 635),
		(t_now - 326, t_now - 320),
		(t_now - 288, t_now - 283),
		(t_now - 273, t_now - 256),
		(t_now - 246, t_now - 237),
		(t_now - 14, t_now),
	]

	def in_request(sec):
		for t0, t1 in intervals:
			if t0 <= sec <= t1:
				return True, t0
		return False, 0

	for sec in range(t_now - 3600, t_now + 1):
		rings["budget"].put(sec, 23040.0)
		if sec < t_load:
			rings["vram_llama"].put(sec, 0.0)
			rings["spill"].put(sec, 0.0)
			rings["gpu_llama"].put(sec, 0.0)
			rings["llama_ws"].put(sec, 0.0)
			rings["ram_avail"].put(sec, max(0.0, min(65536.0, 54200.0 + rng.uniform(-5.0, 5.0))))
			rings["pages_in"].put(sec, 0.0)
			rings["cpu"].put(sec, rng.uniform(4.0, 8.0))
			rings["dec"].put(sec, float("nan"))
			rings["pre"].put(sec, float("nan"))
			rings["ctx"].put(sec, 0.0)
			rings["busy"].put(sec, 0.0)
		elif sec <= t_load + 5:
			# Transient during model load: spill peaks at ~520 MiB before settling
			rings["vram_llama"].put(sec, max(0.0, 11450.0 + rng.uniform(-2.0, 2.0)))
			rings["spill"].put(sec, 520.0)
			rings["llama_ws"].put(sec, max(0.0, 2048.0 + rng.uniform(-3.0, 3.0)))
			rings["ram_avail"].put(sec, max(0.0, min(65536.0, 43520.0 + rng.uniform(-5.0, 5.0))))
			rings["pages_in"].put(sec, 1.0)
			rings["gpu_llama"].put(sec, max(0.0, min(100.0, 85.0 + rng.uniform(-1.0, 1.0))))
			rings["cpu"].put(sec, rng.uniform(45.0, 65.0))
			rings["dec"].put(sec, float("nan"))
			rings["pre"].put(sec, float("nan"))
			rings["ctx"].put(sec, 0.0)
			rings["busy"].put(sec, 0.0)
		else:
			rings["vram_llama"].put(sec, max(0.0, 11450.0 + rng.uniform(-2.0, 2.0)))
			rings["spill"].put(sec, max(0.0, 320.0 + rng.uniform(-2.0, 2.0)))
			rings["llama_ws"].put(sec, max(0.0, 2048.0 + rng.uniform(-3.0, 3.0)))
			rings["ram_avail"].put(sec, max(0.0, min(65536.0, 43520.0 + rng.uniform(-5.0, 5.0))))
			rings["pages_in"].put(sec, 1.0 if (rng.random() < 0.002) else 0.0)

			is_req, t_start = in_request(sec)
			if is_req:
				rings["busy"].put(sec, 1.0)
				rings["gpu_llama"].put(sec, max(0.0, min(100.0, 78.0 + rng.uniform(-1.0, 1.0))))
				rings["dec"].put(sec, max(0.0, 45.2 + rng.uniform(-0.3, 0.3)))
				rings["pre"].put(sec, max(0.0, 480.0 + rng.uniform(-3.0, 3.0)) if sec == t_start else float("nan"))
				rings["ctx"].put(sec, max(0.0, min(32768.0, 2450.0 + (sec - t_start) * 45.0)))
				rings["cpu"].put(sec, max(0.0, min(100.0, 24.0 + rng.uniform(-2.0, 4.0))))
			else:
				rings["busy"].put(sec, 0.0)
				rings["gpu_llama"].put(sec, max(0.0, min(100.0, rng.uniform(0.0, 0.8))))
				rings["dec"].put(sec, float("nan"))
				rings["pre"].put(sec, float("nan"))
				rings["ctx"].put(sec, 2450.0)
				rings["cpu"].put(sec, max(0.0, min(100.0, 16.0 + rng.uniform(-3.0, 4.0))))

	requests = [
		{
			"task": 100,
			"preset": "demo-14b",
			"pn": 1280,
			"cached": 1024,
			"pp": 512.0,
			"tn": 256,
			"tg": 46.1,
			"wall": FIXED_TIME - 320.0,
			"wall0": FIXED_TIME - 326.0,
			"ms": 6100.0,
			"acc": None,
			"sel": "LCP",
			"sim": 0.80,
			"kv": 2560,
		},
		{
			"task": 101,
			"preset": "demo-14b",
			"pn": 450,
			"cached": 2500,
			"pp": 485.0,
			"tn": 180,
			"tg": 45.8,
			"wall": FIXED_TIME - 283.0,
			"wall0": FIXED_TIME - 288.0,
			"ms": 4900.0,
			"acc": 0.90,
			"dacc": 180,
			"dgen": 200,
			"mlen": 1.90,
			"sel": "LCP",
			"keep": 1.0,
			"kv": 3130,
		},
		{
			"task": 102,
			"preset": "demo-14b",
			"pn": 3100,
			"cached": 3050,
			"pp": 530.0,
			"tn": 512,
			"tg": 44.9,
			"wall": FIXED_TIME - 256.0,
			"wall0": FIXED_TIME - 273.0,
			"ms": 17200.0,
			"acc": 0.88,
			"dacc": 458,
			"dgen": 520,
			"mlen": 1.85,
			"sel": "LCP",
			"keep": 1.0,
			"sim": 0.82,
			"kv": 6662,
		},
		{
			"task": 103,
			"preset": "demo-14b",
			"pn": 820,
			"cached": 6200,
			"pp": 495.0,
			"tn": 340,
			"tg": 45.4,
			"wall": FIXED_TIME - 237.0,
			"wall0": FIXED_TIME - 246.0,
			"ms": 9100.0,
			"acc": 0.92,
			"dacc": 322,
			"dgen": 350,
			"mlen": 2.05,
			"sel": "LCP",
			"keep": 1.0,
			"sim": 0.62,
			"kv": 7360,
		},
	]

	events = [
		{"wall": FIXED_TIME - 2520.0, "level": "info", "kind": "model", "msg": "demo-14b loaded in 4.2 s (11.2 GiB VRAM)"},
		{"wall": FIXED_TIME - 1200.0, "level": "info", "kind": "system", "msg": "working set trimmed: 512 MiB reclaimed"},
		{"wall": FIXED_TIME - 35.0, "level": "info", "kind": "model", "msg": "slot 0 assigned to task 104 (LCP 76%)"},
	]

	collector = types.SimpleNamespace(
		requests=requests,
		follow={"demo-14b": (None, types.SimpleNamespace(session=lambda: {"wall": FIXED_TIME - 2520.0}))},
		flags={"demo-14b": ({"ctx-size": "32768", "cache-type-k": "q8_0", "cache-type-v": "q8_0"}, "")},
		events=events,
		cfg={"spill_mib": 256, "router": "http://127.0.0.1:8080"},
		rings=rings,
		alerts=types.SimpleNamespace(ordered=lambda: [], active={}),
		allow=256.0,
		allow_known=True,
	)

	snap = {
		"t": FIXED_TIME,
		"router_up": True,
		"mode": "router",
		"notices": [],
		"gpu": {
			"name": "Demo GPU",
			"vram": 24576.0,
			"budget": 23040.0,
			"ded": 13020.0,
			"shr": 500.0,
		},
		"llama": {
			"present": True,
			"ded": 11450.0,
			"shr": 320.0,
			"floor": 256.0,
			"floor_known": True,
			"allow": 256.0,
			"allow_known": True,
			"allow_src": "measured",
			"eng": {"3D": 78.0, "Compute": 0.0},
			"ws": 2048.0,
			"commit": 12288.0,
			"cpu": 1.2,
			"pm_error": None,
		},
		"others": [
			{"exe": "dwm.exe", "ded": 820.0, "shr": 40.0, "eng": 1.0},
			{"exe": "browser.exe", "ded": 750.0, "shr": 140.0, "eng": 2.0},
		],
		"ram": {
			"total": 65536.0,
			"avail": 43520.0,
			"commit": 28672.0,
			"commit_max": 73728.0,
			"pagefiles": [{"name": "pagefile.sys", "size": 8192.0}],
			"standby": 16384.0,
			"free": 27136.0,
			"pages_in": 0.0,
			"pages_out": 0.0,
		},
		"cpu": {
			"cpu": 18.0,
			"cpu_mhz": 4400.0,
			"cpu_queue": 0.0,
			"cpu_cores": [16.0, 25.0, 11.0, 30.0, 7.0, 19.0, 27.0, 5.0, 14.0, 33.0, 9.0, 21.0, 17.0, 6.0, 29.0, 19.0],
		},
		"presets": {
			"demo-14b": {
				"status": "loaded",
				"phase": "decode",
				"meta": {"ftype": "Q4_K_M", "n_params": 14.7e9},
				"elapsed": 13.6,
				"rate_dec": 45.2,
				"rate_pre": 480.0,
				"eta": float("nan"),
				"kv": 3062,
				"slot": {
					"task": 104,
					"decoded": 612,
					"remain": 412,
					"n_ctx": 32768,
					"n_prompt": 2450,
					"cached": 1850,
					"done": 600,
					"todo": 600,
					"params": {
						"temperature": 0.7,
						"top_p": 0.9,
						"max_tokens": 1024,
						"top_k": 40,
						"min_p": 0.05,
						"reasoning_format": "chatml",
					},
				},
				"slot_error": None,
			}
		},
	}

	return collector, snap


def render_frame_lines(c, snap, width=120):
	ui.C.enable(True)
	ui.STYLE["mode"] = "blocks"
	view = pages.View(3)  # 1h time window

	body = pages.live(c, snap, view, width, 55)
	lines = [pages.header(c, snap, view, width)]
	lines += pages.banner(c, snap, 4, width)
	lines += body
	lines.append(pages.footer(view, width))
	return [ui.clip(ln, width - 1) for ln in lines]


def parse_line_spans(raw_line):
	"""Parse an ANSI string into chunks with styling and column coordinates."""
	state = {"fg": None, "bg": None, "bold": False, "dim": False, "rev": False}
	pos = 0
	col = 0
	spans = []

	for match in RE_SGR.finditer(raw_line):
		start, end = match.span()
		if start > pos:
			text = raw_line[pos:start]
			spans.append({
				"text": text,
				"col": col,
				"fg": state["fg"],
				"bg": state["bg"],
				"bold": state["bold"],
				"dim": state["dim"],
				"rev": state["rev"],
			})
			col += len(text)
		seq = match.group(1)
		codes = [int(x) for x in seq.split(";") if x] if seq else [0]
		i = 0
		while i < len(codes):
			code = codes[i]
			if code == 0:
				state["fg"] = None
				state["bg"] = None
				state["bold"] = False
				state["dim"] = False
				state["rev"] = False
			elif code == 1:
				state["bold"] = True
			elif code == 2:
				state["dim"] = True
			elif code == 7:
				state["rev"] = True
			elif code == 22:
				state["bold"] = False
				state["dim"] = False
			elif code == 27:
				state["rev"] = False
			elif 30 <= code <= 37:
				state["fg"] = PALETTE_STANDARD[code - 30]
			elif code == 38:
				if i + 2 < len(codes) and codes[i + 1] == 5:
					state["fg"] = get_256_color(codes[i + 2])
					i += 2
				elif i + 4 < len(codes) and codes[i + 1] == 2:
					state["fg"] = "#%02x%02x%02x" % (codes[i + 2], codes[i + 3], codes[i + 4])
					i += 4
			elif code == 39:
				state["fg"] = None
			elif 40 <= code <= 47:
				state["bg"] = PALETTE_STANDARD[code - 40]
			elif code == 48:
				if i + 2 < len(codes) and codes[i + 1] == 5:
					state["bg"] = get_256_color(codes[i + 2])
					i += 2
				elif i + 4 < len(codes) and codes[i + 1] == 2:
					state["bg"] = "#%02x%02x%02x" % (codes[i + 2], codes[i + 3], codes[i + 4])
					i += 4
			elif code == 49:
				state["bg"] = None
			elif 90 <= code <= 97:
				state["fg"] = PALETTE_BRIGHT[code - 90]
			elif 100 <= code <= 107:
				state["bg"] = PALETTE_BRIGHT[code - 100]
			i += 1
		pos = end

	if pos < len(raw_line):
		text = raw_line[pos:]
		spans.append({
			"text": text,
			"col": col,
			"fg": state["fg"],
			"bg": state["bg"],
			"bold": state["bold"],
			"dim": state["dim"],
			"rev": state["rev"],
		})

	# Merge adjacent spans with identical visual styling
	merged = []
	for sp in spans:
		if not sp["text"]:
			continue
		if (
			merged
			and sp["fg"] == merged[-1]["fg"]
			and sp["bg"] == merged[-1]["bg"]
			and sp["bold"] == merged[-1]["bold"]
			and sp["dim"] == merged[-1]["dim"]
			and sp["rev"] == merged[-1]["rev"]
		):
			merged[-1]["text"] += sp["text"]
		else:
			merged.append(sp)
	return merged


def escape_xml(s):
	return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def ansi_to_svg(lines, width=120):
	char_w = 7.8
	line_h = 18.0
	pad_x = 18.0
	title_h = 34.0
	pad_y = 12.0

	content_w = round(width * char_w)
	svg_w = content_w + int(pad_x * 2)
	content_h = int(len(lines) * line_h)
	svg_h = int(title_h + pad_y * 2 + content_h)

	bg_rects = []
	text_lines = []

	for row_idx, raw_line in enumerate(lines):
		spans = parse_line_spans(raw_line)
		y_top = title_h + pad_y + row_idx * line_h
		y_base = y_top + 13.5

		tspan_parts = []
		for sp in spans:
			eff_fg = sp["fg"] or DEFAULT_FG
			eff_bg = sp["bg"]
			if sp["rev"]:
				eff_fg, eff_bg = (eff_bg or DEFAULT_BG), (sp["fg"] or DEFAULT_FG)

			if eff_bg is not None:
				rect_x = pad_x + sp["col"] * char_w
				rect_w = len(sp["text"]) * char_w
				bg_rects.append(
					'<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" rx="2"/>'
					% (rect_x, y_top + 1.0, rect_w, line_h - 2.0, eff_bg)
				)

			x_pos = pad_x + sp["col"] * char_w
			attrs = ['x="%.1f"' % x_pos, 'fill="%s"' % eff_fg]
			if sp["bold"]:
				attrs.append('font-weight="bold"')
			if sp["dim"] and not sp["fg"]:
				attrs.append('opacity="0.7"')
			escaped = escape_xml(sp["text"])
			tspan_parts.append('<tspan %s>%s</tspan>' % (" ".join(attrs), escaped))

		if tspan_parts:
			text_lines.append(
				'<text y="%.1f" xml:space="preserve">%s</text>'
				% (y_base, "".join(tspan_parts))
			)

	svg_parts = [
		'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d">'
		% (svg_w, svg_h, svg_w, svg_h),
		'<style>',
		'text {',
		'\tfont-family: "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace;',
		'\tfont-size: 13px;',
		'\twhite-space: pre;',
		'}',
		'</style>',
		'<rect width="%d" height="%d" rx="8" ry="8" fill="%s" stroke="#2c313a" stroke-width="1"/>'
		% (svg_w, svg_h, DEFAULT_BG),
		'<circle cx="20" cy="17" r="5.5" fill="#e06c75"/>',
		'<circle cx="36" cy="17" r="5.5" fill="#e5c07b"/>',
		'<circle cx="52" cy="17" r="5.5" fill="#98c379"/>',
		'<text x="%d" y="21" text-anchor="middle" fill="#6c7585" font-size="12" font-weight="bold">win-llmtop</text>'
		% (svg_w // 2),
		'<line x1="0" y1="%.1f" x2="%d" y2="%.1f" stroke="#2c313a" stroke-width="1"/>'
		% (title_h, svg_w, title_h),
	]
	svg_parts.extend(bg_rects)
	svg_parts.extend(text_lines)
	svg_parts.append("</svg>")
	return "\n".join(svg_parts) + "\n"


def main():
	ap = argparse.ArgumentParser(description="Generate reproducible SVG screenshot of win-llmtop Live page")
	ap.add_argument("--out", default=DEFAULT_OUT, help="path for output SVG")
	ap.add_argument("--text", action="store_true", help="print plain text frame to stdout")
	ap.add_argument("--width", type=int, default=120, help="frame width in columns")
	args = ap.parse_args()

	c, snap = make_demo_state()
	lines = render_frame_lines(c, snap, width=args.width)

	if args.text:
		plain_lines = [ui.RE_ANSI.sub("", ln) for ln in lines]
		print("\n".join(plain_lines))
		return

	svg = ansi_to_svg(lines, width=args.width)
	out_path = os.path.abspath(args.out)
	os.makedirs(os.path.dirname(out_path), exist_ok=True)
	with open(out_path, "w", encoding="utf-8", newline="\n") as f:
		f.write(svg)


if __name__ == "__main__":
	main()
