"""Parse an agent's final JSON answer and compare it with the answer key."""

from __future__ import annotations

import ast
import json
import re
from typing import Any

REL_TOL = 0.005  # ±0.5% for non-integer expected values
ABS_TOL = 0.01


def parse_answer(text: str) -> tuple[Any, str | None]:
    """Return (answer, error). Scans every '{' and decodes the JSON object that
    starts there; returns the LAST object that has an 'answer' key. Handles
    fences, prose around the JSON, and responses that repeat the object."""
    if not text:
        return None, "empty response"
    decoder = json.JSONDecoder()
    found = None
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            obj = _python_literal(text, m.start())
            if obj is None:
                continue
        if isinstance(obj, dict) and "answer" in obj:
            found = obj
    if found is None:
        return None, "no JSON object with an 'answer' key"
    return found["answer"], None


def _python_literal(text: str, start: int):
    """Accept a Python-style dict ({'answer': ...}) — some replies use single quotes."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return ast.literal_eval(text[start : i + 1])
                except (ValueError, SyntaxError):
                    return None
    return None


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(',', '').replace('$', '').strip()
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _norm_str(v: Any) -> str:
    return ' '.join(str(v).strip().strip('.').lower().split())


def is_correct(answer: Any, expected: Any, answer_type: str) -> bool:
    if answer is None:
        return False
    if answer_type == "number":
        a, e = _num(answer), _num(expected)
        if a is None or e is None:
            return False
        if isinstance(expected, int):  # counts and cent totals: exact
            return abs(a - e) < 0.5
        return abs(a - e) <= max(ABS_TOL, REL_TOL * abs(e))  # averages: ±0.5%
    if answer_type == 'string':
        if isinstance(answer, list) and len(answer) == 1:
            answer = answer[0]
        if _num(expected) is not None and _num(answer) is not None:
            return _num(answer) == _num(expected)
        return _norm_str(answer) == _norm_str(expected)
    if answer_type in {'list', 'set'}:
        if isinstance(answer, str):
            answer = [x for x in re.split(r'\s*[,;\n]\s*', answer) if x]
        if not isinstance(answer, list):
            answer = [answer]
        a = [_norm_str(x) for x in answer]
        e = [_norm_str(x) for x in expected]
        return a == e if answer_type == 'list' else sorted(a) == sorted(e)
    return False
