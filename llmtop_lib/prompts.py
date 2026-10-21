# SPDX-License-Identifier: GPL-3.0-or-later
"""Group llama-server turns (requests) into multi-turn prompts and compute per-prompt figures.

Pure functions over turn dicts produced by `llmtop_lib.log.Assembler`; no I/O.
"""

NAN = float("nan")


def _pms(r):
	if "pms" in r and r["pms"] is not None:
		return float(r["pms"])
	pn, pp = r.get("pn", 0), r.get("pp", NAN)
	return 1000.0 * pn / pp if pn > 0 and pp == pp and pp > 0 else 0.0


def _tms(r):
	if "tms" in r and r["tms"] is not None:
		return float(r["tms"])
	tn, tg = r.get("tn", 0), r.get("tg", NAN)
	return 1000.0 * tn / tg if tn > 0 and tg == tg and tg > 0 else 0.0


def _wall0(r):
	if r.get("wall0") is not None:
		return r["wall0"]
	if r.get("wall") is not None:
		return r["wall"] - r.get("ms", 0.0) / 1e3
	return None


def _split(a, b, gap_s):
	"""True when turn `b` starts a new prompt after turn `a` on the same preset and slot."""
	if a.get("session") != b.get("session"):
		return True
	if b.get("sel") == "LRU" and (b.get("evicted") or 0) > 0:
		return True
	keep = b.get("keep")
	if keep is None and a.get("kv", 0) > 0 and b.get("cached") is not None:
		keep = b["cached"] / a["kv"]
	if keep is not None and keep < 0.5:
		return True
	a_wall, b_wall0 = a.get("wall"), _wall0(b)
	if a_wall is not None and b_wall0 is not None:
		return (b_wall0 - a_wall) >= gap_s
	a_t, b_t0 = a.get("t"), b.get("t0")
	if a_t is not None and b_t0 is not None and (a_t > 0 or b_t0 > 0):
		return (b_t0 - a_t) >= gap_s
	return False


def _summarize(ts):
	last_r = ts[-1]
	preset = ts[0]["preset"]
	slot = next((r.get("slot") for r in reversed(ts) if r.get("slot") is not None), None)
	session = last_r.get("session")
	w0 = _wall0(ts[0])
	w1 = last_r.get("wall")
	t0, t = ts[0].get("t0"), last_r.get("t")
	kv = last_r["kv"] if "kv" in last_r else (last_r.get("cached") or 0) + last_r.get("pn", 0) + last_r.get("tn", 0)
	pn = sum(r.get("pn", 0) for r in ts)
	cached = None if all(r.get("cached") is None for r in ts) else sum(r.get("cached") or 0 for r in ts)
	tn = sum(r.get("tn", 0) for r in ts)
	pms = sum(_pms(r) for r in ts)
	tms = sum(_tms(r) for r in ts)
	pp = 1000.0 * pn / pms if pn > 0 and pms > 0 else NAN
	tg = 1000.0 * tn / tms if tn > 0 and tms > 0 else NAN
	ms = sum(r.get("ms", 0.0) for r in ts)
	if w0 is not None and w1 is not None:
		wall_s = max(0.0, w1 - w0)
	elif t0 is not None and t is not None and (t > 0 or t0 > 0) and ts[0].get("session") == last_r.get("session"):
		wall_s = max(0.0, t - t0)
	else:
		wall_s = NAN
	evicted = sum(r.get("evicted", 0) for r in ts if r.get("sel") == "LRU" and r.get("evicted", 0) > 0)
	trunc = any(r.get("trunc") for r in ts)
	reallocs = sum(r.get("reallocs", 0) for r in ts)
	ch = [r for r in ts if r.get("cached") is not None]
	cden = sum(r["cached"] + r.get("pn", 0) for r in ch)
	hit = sum(r["cached"] for r in ch) / cden if cden > 0 else NAN
	dacc = sum(r.get("dacc", 0) for r in ts if "dacc" in r)
	dgen = sum(r.get("dgen", 0) for r in ts if "dgen" in r)
	if dgen > 0:  # per-turn draft counts, when the log has them
		acc = dacc / dgen
	else:  # older history: weight the per-turn acceptance rates by generated tokens
		an = [r for r in ts if r.get("acc") is not None]
		aden = sum(r.get("tn", 0) for r in an)
		acc = sum(r["acc"] * r.get("tn", 0) for r in an) / aden if aden > 0 else None
	issues = []
	if evicted > 0:
		issues.append("cache_reset")
	if trunc:
		issues.append("trunc")
	if reallocs > 0:
		issues.append("reallocs")
	return {
		"preset": preset,
		"slot": slot,
		"session": session,
		"turns": ts,
		"n": len(ts),
		"wall0": w0,
		"wall": w1,
		"t0": t0,
		"t": t,
		"kv": kv,
		"context": kv,
		"pn": pn,
		"cached": cached,
		"tn": tn,
		"pms": pms,
		"tms": tms,
		"pp": pp,
		"tg": tg,
		"ms": ms,
		"busy": ms / 1e3,
		"wall_s": wall_s,
		"evicted": evicted,
		"trunc": trunc,
		"reallocs": reallocs,
		"issues": issues,
		"hit": hit,
		"acc": acc,
	}


def group(turns, gap_s=30):
	"""Group chronological `turns` into prompts. Returns a list of prompt dicts in prompt-start order."""
	groups = []
	active = {}  # (preset, slot) -> list of turns in the open prompt
	for r in turns:
		preset = r["preset"]
		slot = r.get("slot")
		key = (preset, slot)
		prev = active.get(key)
		if prev is None and slot is not None and (preset, None) in active:
			prev = active.pop((preset, None))
		elif prev is None and slot is None:
			slots = [k for k in active if k[0] == preset]
			if len(slots) == 1:
				key = slots[0]
				prev = active[key]
		if prev is None or _split(prev[-1], r, gap_s):
			cur = [r]
			groups.append(cur)
			active[key] = cur
		else:
			prev.append(r)
			active[key] = prev
	return [_summarize(ts) for ts in groups]
