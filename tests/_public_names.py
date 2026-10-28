# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Aaron Kane <software@kanenet.us>
"""Shared forbidden-content patterns for public-tree and documentation tests.

	Built from string pieces so this module never matches its own patterns.
"""

import re

WORDS = (
	"kane" + "net",
	"pv" + "t.",
	"qw" + "en",
	"gem" + "ma",
)

LLM_DRIVE_PREFIX = "D:" + "\\" + "LL" + "M"
FORBIDDEN = (LLM_DRIVE_PREFIX,) + WORDS

PATTERNS = (
	("llm_drive_path", re.compile(r"[A-Za-z]:" + r"\\" + "LL" + "M", re.I)),
) + tuple((w, re.compile(re.escape(w), re.I)) for w in WORDS)
