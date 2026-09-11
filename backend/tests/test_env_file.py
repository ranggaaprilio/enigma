from app.services.env_file import ParseProblem, parse_env_text, serialize_env


def _as_dict(lines):
    return {line.key: line.value for line in lines}


def test_export_prefix_comments_and_blank_lines_are_handled():
    text = """
# a leading comment

export FOO=bar
  # indented comment
BAZ=qux
"""
    lines, problems = parse_env_text(text)

    assert _as_dict(lines) == {"FOO": "bar", "BAZ": "qux"}
    assert problems == []


def test_single_quoted_value_is_literal_with_no_interpolation():
    text = "TOKEN='raw $HOME value with \\\\n literal backslash-n'\n"

    lines, problems = parse_env_text(text)

    assert _as_dict(lines) == {"TOKEN": "raw $HOME value with \\\\n literal backslash-n"}
    assert problems == []


def test_double_quoted_escapes_and_genuine_multiline_value():
    text = 'A="line one\\nstill escaped"\nB="real\nnewline"\nC="tab\\there"\n'

    lines, problems = parse_env_text(text)
    values = _as_dict(lines)

    assert values["A"] == "line one\nstill escaped"
    assert values["B"] == "real\nnewline"
    assert values["C"] == "tab\there"
    assert problems == []


def test_double_quoted_backslash_and_quote_escapes():
    text = 'KEY="a\\\\b said \\"hi\\""\n'

    lines, _problems = parse_env_text(text)

    assert _as_dict(lines) == {"KEY": 'a\\b said "hi"'}


def test_inline_comment_only_recognised_after_whitespace():
    text = "GLUED=abc#123\nSPACED=abc #123\nSTARTS=#nothing\n"

    lines, _problems = parse_env_text(text)
    values = _as_dict(lines)

    assert values["GLUED"] == "abc#123"
    assert values["SPACED"] == "abc"
    # A '#' immediately after '=' (nothing whitespace precedes it) is not a comment starter.
    assert values["STARTS"] == "#nothing"


def test_invalid_lines_produce_problems_without_leaking_the_value():
    secret = "super-secret-value-do-not-log"
    text = f"NOTANASSIGNMENT\n1BAD={secret}\n"

    lines, problems = parse_env_text(text)

    assert lines == []
    assert len(problems) == 2
    assert problems[0] == ParseProblem(1, "expected KEY=value")
    assert problems[1] == ParseProblem(2, "invalid key")
    for problem in problems:
        assert secret not in problem.message


def test_unterminated_single_quote_is_a_problem_not_a_crash():
    text = "KEY='never closed\nOTHER=fine\n"

    lines, problems = parse_env_text(text)

    assert any(p.line_no == 1 and "unterminated" in p.message for p in problems)
    assert "KEY" not in _as_dict(lines)


def test_unterminated_double_quote_is_a_problem():
    text = 'KEY="never closed\n'

    lines, problems = parse_env_text(text)

    assert lines == []
    assert any("unterminated" in p.message for p in problems)


def test_duplicate_keys_last_value_wins_with_a_warning():
    text = "DUP=1\nDUP=2\nDUP=3\n"

    lines, problems = parse_env_text(text)

    assert _as_dict(lines) == {"DUP": "3"}
    assert any(
        p.line_no == 3 and p.message == "DUP appears 3 times; last value used" for p in problems
    )


def test_crlf_and_bom_are_normalised():
    text = "﻿FOO=bar\r\nBAZ=qux\r\n"

    lines, problems = parse_env_text(text)

    assert _as_dict(lines) == {"FOO": "bar", "BAZ": "qux"}
    assert problems == []


def test_serialize_env_output_is_sorted_and_header_is_a_comment():
    text = serialize_env({"ZETA": "1", "ALPHA": "2"})

    lines = text.splitlines()
    assert lines[0].startswith("#")
    body = [line for line in lines[1:] if line]
    assert body == ["ALPHA=2", "ZETA=1"]


def test_serialize_env_round_trips_tricky_values():
    values = {
        "PLAIN": "value",
        "EMPTY": "",
        "WITH_SPACE": "has space",
        "WITH_SINGLE_QUOTE": "it's a test",
        "WITH_DOUBLE_QUOTE": 'he said "hi"',
        "WITH_BOTH_QUOTES": 'it\'s "both"',
        "WITH_DOLLAR": "a$b",
        "WITH_NEWLINE": "line1\nline2",
        "WITH_BACKSLASH": "a\\b",
        "WITH_TAB": "a\tb",
    }

    text = serialize_env(values)
    lines, problems = parse_env_text(text)

    assert problems == []
    assert _as_dict(lines) == values


def test_serialize_env_bare_values_use_no_quoting():
    text = serialize_env({"KEY": "simple-value_123.foo/bar:baz@qux+1,2=3%4"})

    assert "KEY=simple-value_123.foo/bar:baz@qux+1,2=3%4\n" in text
    assert "'" not in text.split("\n", 1)[1]
    assert '"' not in text
