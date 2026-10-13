import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.score import is_correct, parse_answer  # noqa: E402


@pytest.mark.parametrize(
    'text,expected',
    [
        ('{"answer": 42, "explanation": "x"}', 42),
        (
            '```json\n{"answer": "Brooklyn", "explanation": "x"}\n```',
            'Brooklyn',
        ),
        ('Sure. {"answer": [1, 2], "explanation": "x"}', [1, 2]),
    ],
)
def test_parse(text, expected):
    assert parse_answer(text) == (expected, None)


def test_parse_repeated_objects():
    text = 'Command output: `{"answer": 1594734, "explanation": "a"}`\n{"answer": 1594734, "explanation": "b"}'
    assert parse_answer(text) == (1594734, None)


def test_parse_failure():
    assert parse_answer('no json here')[1] is not None


@pytest.mark.parametrize(
    'answer,expected,kind,ok',
    [
        (66375, 66375, 'number', True),
        ('66,375', 66375, 'number', True),
        (1123.38, 1123.38, 'number', True),
        (1123, 1123.38, 'number', True),  # within 0.5%
        (1200, 1123.38, 'number', False),
        (66100, 66375, 'number', False),  # integer answers are exact
        ('brooklyn.', 'Brooklyn', 'string', True),
        (['Brooklyn'], 'Brooklyn', 'string', True),
        ('8', 8, 'string', True),
        (['A', 'B'], ['a', 'b'], 'list', True),
        (['B', 'A'], ['a', 'b'], 'list', False),
        (['B', 'A'], ['a', 'b'], 'set', True),
        ('Los Angeles', ['Los Angeles'], 'set', True),
        (None, 1, 'number', False),
    ],
)
def test_is_correct(answer, expected, kind, ok):
    assert is_correct(answer, expected, kind) is ok


def test_parse_python_style_dict():
    assert parse_answer("0{'answer': ['Los Angeles'], 'explanation': 'x'}") == (["Los Angeles"], None)
