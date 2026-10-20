# SPDX-License-Identifier: GPL-3.0-or-later
"""Terminal drawing primitives for llmtop: colours, bars, sparklines, ANSI-aware clipping."""

import re

RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
RE_TOKEN = re.compile(r"(\x1b\[[0-9;?]*[A-Za-z])|(.)")
SPARK = "▁▂▃▄▅▆▇"
PARTIAL = " ▏▎▍▌▋▊▉"


class _Colors:
	"""ANSI codes; all empty when colour is off."""

	def __init__(self):
		self.enable(True)

	def enable(self, on):
		codes = dict(reset=0, bold=1, dim=2, rev=7, red=31, green=32, yellow=33, blue=34, magenta=35, cyan=36, grey=90, bred=91, bgreen=92, byellow=93, bcyan=96)
		for name, n in codes.items():
			setattr(self, name, "\x1b[%dm" % n if on else "")


C = _Colors()


def vlen(s):
	return len(RE_ANSI.sub("", s))


def clip(s, width):
	"""Truncate to `width` visible columns, keeping colour codes intact."""
	if vlen(s) <= width:
		return s
	out, n = [], 0
	for m in RE_TOKEN.finditer(s):
		if m.group(1):
			out.append(m.group(1))
		elif n < width - 1:
			out.append(m.group(2))
			n += 1
		else:
			break
	return "".join(out) + "…" + C.reset


def pad(s, width):
	return s + " " * max(0, width - vlen(s))


def rpad(s, width):
	return " " * max(0, width - vlen(s)) + s


def bar(frac, width, color=""):
	"""Horizontal bar with eighth-cell resolution."""
	frac = 0.0 if frac != frac else min(1.0, max(0.0, frac))
	cells = frac * width
	full = int(cells)
	part = int((cells - full) * 8) if full < width else 0
	fill = "█" * full + (PARTIAL[part] if part else "")
	return "%s%s%s%s%s" % (color, fill, C.grey, "░" * (width - len(fill)), C.reset)


def stack(parts, total, width, marker=None):
	"""Stacked bar: parts is [(value, colour)], total is the full scale; `marker` draws a ┃ at that value."""
	cells, used, acc = [], 0, 0.0
	for value, color in parts:
		acc += max(0.0, value)
		end = min(width, round(acc / total * width)) if total else 0
		cells += [color + "█"] * (end - used)
		used = max(used, end)
	cells += [C.grey + "░"] * (width - used)
	if marker is not None and total and 0 <= marker <= total:
		cells[min(width - 1, int(marker / total * width))] = C.bold + C.bred + "┃"
	return "".join(c + C.reset for c in cells)


def chart(vals, lo, hi, rows, color="", dim_floor=False):
	"""`rows` lines (top first) of a column chart with eight steps per row. NaN is blank with a dim dot on the bottom line. The tallest column
	stops an eighth short of the top, so charts stacked above one another never touch."""
	span = (hi - lo) or 1.0
	steps = rows * 8 - 1
	levels = [None if v != v else min(steps, max(1, int((v - lo) / span * steps + 0.5))) for v in vals]
	lines = []
	for r in range(rows):
		below = (rows - 1 - r) * 8
		cells = []
		for lv in levels:
			if lv is None:
				cells.append(C.grey + "·" + C.reset if r == rows - 1 else " ")
				continue
			k = lv - below
			if k <= 0:
				cells.append(" ")
			else:
				cells.append((C.grey if dim_floor and lv <= 1 else color) + ("█" if k >= 8 else SPARK[k - 1]) + C.reset)
		lines.append("".join(cells))
	return lines


STYLE = {"mode": "braille"}
DOTS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))  # braille dot bit by [column][row from the top]


def braille(vals, lo, hi, rows, color="", fill=False):
	"""`rows` lines of a braille plot: two data points per character, four dot rows per line. A line trace, or the area under it when `fill`.
	NaN breaks the line. The result is `rows` strings, top line first."""
	span = (hi - lo) or 1.0
	top = rows * 4 - 1
	masks = [[0] * ((len(vals) + 1) // 2) for _ in range(rows)]
	prev = None
	for x, v in enumerate(vals):
		if v != v:
			prev = None
			continue
		y = min(top, max(0, int((v - lo) / span * top + 0.5)))
		for yy in range(0 if fill else min(y, y if prev is None else prev), y + 1 if fill else max(y, y if prev is None else prev) + 1):
			masks[rows - 1 - yy // 4][x // 2] |= DOTS[x % 2][3 - yy % 4]
		prev = y
	return [color + "".join(chr(0x2800 + m) if m else " " for m in row) + C.reset for row in masks]


def plot(vals, lo, hi, rows, color="", dim_floor=False, fill=False):
	"""Braille line or area plot, or the block-character chart when the style is set to blocks."""
	if STYLE["mode"] == "braille":
		return braille(vals, lo, hi, rows, color, fill)
	return chart(vals, lo, hi, rows, color, dim_floor)


def shade(frac, warn=0.8, crit=0.95):
	return C.bred if frac >= crit else C.byellow if frac >= warn else C.bgreen


def num(x):
	return "{:,.0f}".format(x)


def tok(x):
	return "{:,}".format(int(x))


def gib(mib):
	return "%.1f GiB" % (mib / 1024)


def rate(x, digits=0):
	return "   -  " if x != x else ("%.*f" % (digits, x)).rjust(6)


def ago(seconds):
	seconds = int(max(0, seconds))
	if seconds < 90:
		return "%ds" % seconds
	if seconds < 5400:
		return "%dm%02ds" % (seconds // 60, seconds % 60)
	return "%dh%02dm" % (seconds // 3600, seconds // 60 % 60)
