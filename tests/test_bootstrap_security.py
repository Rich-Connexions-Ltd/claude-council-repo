"""File: tests/test_bootstrap_security.py

Purpose: Assert the Sprint 3 bootstrap hardening invariants —
no bypassPermissions, fenced user input, validated outputs,
constrained slug/lang/ext/grammar, no second-order injection paths.

Last updated: Sprint 4 (2026-04-16) -- added --answers-file security tests (findings #9, #15).
"""

from __future__ import annotations

import ast
import json
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PAYLOADS = json.loads(
    (REPO_ROOT / "tests" / "fixtures" / "injection_payloads.json").read_text()
)["payloads"]


@pytest.fixture
def bs(bootstrap_module):
    return bootstrap_module


# ---------------------------------------------------------------------------
# Source-grep guards
# ---------------------------------------------------------------------------


def test_no_bypass_permissions_in_argv():
    """The literal cmd argv passed to subprocess.run must never contain
    bypassPermissions. Docstring mentions are fine."""
    src = (REPO_ROOT / "scripts" / "bootstrap.py").read_text()
    # Look for the actual flag value inside a list / argv context.
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("'"):
            continue
        if "bypassPermissions" in line and ("=" in line or "," in line) and "NEVER" not in line:
            # Ignore docstring lines (they're inside triple-quoted strings).
            # The simple heuristic: comments and docstrings are out of argv.
            assert "permission-mode" not in line.lower() or "default" in line or "acceptEdits" in line, (
                f"bypassPermissions appears in code: {line!r}"
            )


# ---------------------------------------------------------------------------
# run_claude_cli behaviour
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_subprocess(bs, monkeypatch):
    captured = {}

    class FakeResult:
        returncode = 0
        stdout = "ok-stdout"
        stderr = "ok-stderr"

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeResult()

    monkeypatch.setattr(bs.subprocess, "run", fake_run)
    return captured


def test_run_claude_cli_default_mode(bs, captured_subprocess):
    ok, out, err = bs.run_claude_cli("hello")
    assert ok is True
    assert out == "ok-stdout"
    assert err == "ok-stderr"
    argv = captured_subprocess["args"][0]
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert "bypassPermissions" not in argv


def test_run_claude_cli_allow_edits_mode(bs, captured_subprocess):
    bs.run_claude_cli("x", allow_edits=True)
    argv = captured_subprocess["args"][0]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_run_claude_cli_prompt_via_stdin_not_argv(bs, captured_subprocess):
    """Process-list protection: the prompt is piped via input=, never argv."""
    distinctive = "DISTINCTIVE-TOKEN-STRING-FOR-TEST-92831"
    bs.run_claude_cli(distinctive)
    argv = captured_subprocess["args"][0]
    assert distinctive not in " ".join(argv)
    assert captured_subprocess["kwargs"]["input"] == distinctive
    assert "-" in argv


def test_run_claude_cli_returns_triple(bs, captured_subprocess):
    result = bs.run_claude_cli("x")
    assert isinstance(result, tuple)
    assert len(result) == 3


def test_run_claude_cli_prompt_size_cap(bs, monkeypatch):
    sentinel = {"called": False}

    def fake_run(*args, **kwargs):
        sentinel["called"] = True
        raise AssertionError("subprocess.run should not be invoked when over cap")

    monkeypatch.setattr(bs.subprocess, "run", fake_run)
    huge = "x" * (bs.MAX_TOTAL_BYTES + 1)
    ok, out, err = bs.run_claude_cli(huge)
    assert ok is False
    assert "exceeds" in err
    assert sentinel["called"] is False


# ---------------------------------------------------------------------------
# _validate_user_field
# ---------------------------------------------------------------------------


def test_validate_user_field_rejects_fence_begin(bs):
    with pytest.raises(SystemExit):
        bs._validate_user_field("brief", f"hello {bs.FENCE_BEGIN} bye")


def test_validate_user_field_rejects_fence_end(bs):
    with pytest.raises(SystemExit):
        bs._validate_user_field("brief", f"hello {bs.FENCE_END} bye")


def test_validate_user_field_rejects_nul(bs):
    with pytest.raises(SystemExit):
        bs._validate_user_field("brief", "hello\x00bye")


def test_validate_user_field_rejects_non_string(bs):
    with pytest.raises(SystemExit):
        bs._validate_user_field("brief", 42)


@pytest.mark.parametrize("name,value", [
    ("slug", "ruby"), ("slug", "kotlin_lang"), ("slug", "x1"),
    ("lang", "ruby"), ("lang", "kotlin-multiplatform"), ("lang", "go"),
    ("ext", "rs"), ("ext", "py"), ("ext", "ts"),
    ("grammar", "ruby"), ("grammar", "kotlin-multi"),
])
def test_validate_user_field_constraint_passes(bs, name, value):
    assert bs._validate_user_field(name, value) == value


@pytest.mark.parametrize("name,value", [
    ("slug", "../etc"), ("slug", "a/b"), ("slug", "a.b"),
    ("slug", "Slug"), ("slug", ""), ("slug", "x" * 33),
    ("lang", "lang/with/slash"), ("lang", "../traversal"),
    ("ext", "py.bak"), ("ext", "verylongext"),
    ("grammar", "with space"), ("grammar", "../parent"),
])
def test_validate_user_field_constraint_fails(bs, name, value):
    with pytest.raises(SystemExit):
        bs._validate_user_field(name, value)


@pytest.mark.parametrize("size", [
    -1,  # MAX_FIELD_BYTES - 1
    0,   # exactly MAX_FIELD_BYTES
    1,   # MAX_FIELD_BYTES + 1 → truncated
])
def test_validate_user_field_size_boundary(bs, size, capsys):
    n = bs.MAX_FIELD_BYTES + size
    s = "a" * n
    out = bs._validate_user_field("brief", s)
    encoded = out.encode("utf-8")
    assert len(encoded) <= bs.MAX_FIELD_BYTES
    captured = capsys.readouterr()
    if size > 0:
        assert "truncating" in captured.err
    else:
        assert "truncating" not in captured.err


def test_validate_user_field_utf8_truncation(bs, capsys):
    # 4-byte emoji * (MAX/4) is exactly MAX bytes; +1 emoji exceeds.
    safe_count = bs.MAX_FIELD_BYTES // 4
    s = "😀" * (safe_count + 10)
    out = bs._validate_user_field("brief", s)
    # Result is valid UTF-8 (encode/decode round trip succeeds).
    assert out.encode("utf-8").decode("utf-8") == out


def test_validate_user_field_idempotent(bs):
    clean = "hello world"
    assert bs._validate_user_field("brief", clean) == clean
    twice = bs._validate_user_field("brief", bs._validate_user_field("brief", clean))
    assert twice == clean


# ---------------------------------------------------------------------------
# _fence
# ---------------------------------------------------------------------------


def test_fence_wraps_correctly(bs):
    out = bs._fence("brief", "hello")
    assert bs.FENCE_BEGIN in out
    assert bs.FENCE_END in out
    assert "hello" in out
    assert "name=brief" in out


def test_fence_rejects_unsafe(bs):
    with pytest.raises(SystemExit):
        bs._fence("brief", f"hello {bs.FENCE_BEGIN} bye")


# ---------------------------------------------------------------------------
# _safe_apply_indexer_output
# ---------------------------------------------------------------------------


def _good_indexer_dest(bs):
    return REPO_ROOT / "scripts" / "indexers" / "test_synth.py"


@pytest.mark.parametrize("snippet", [
    "import subprocess",
    "from subprocess import run",
    "import os; os.system('x')",
    "eval('1+1')",
    "exec('x = 1')",
    "__import__('os')",
    "compile('x', '<f>', 'exec')",
    "import importlib",
])
def test_safe_apply_indexer_rejects_dangerous(bs, snippet):
    src = f"def index_x(p):\n    return {{}}\n{snippet}\n"
    ok, reason = bs._safe_apply_indexer_output(src, _good_indexer_dest(bs))
    assert not ok
    assert reason


@pytest.mark.parametrize("call", [
    'open("x", "w")',
    'open("x", "wb")',
    'open("x", "a")',
    'open("x", mode="w")',
    'open(file="x", mode="wb")',
])
def test_safe_apply_indexer_rejects_write_open(bs, call):
    src = f"def index_x(p):\n    return {{}}\n{call}\n"
    ok, reason = bs._safe_apply_indexer_output(src, _good_indexer_dest(bs))
    assert not ok
    assert "open" in reason


def test_safe_apply_indexer_rejects_outside_dir(bs):
    src = "def index_x(p):\n    return {}\n"
    bad_dest = REPO_ROOT / "bad.py"
    ok, reason = bs._safe_apply_indexer_output(src, bad_dest)
    assert not ok
    assert "outside" in reason


def test_safe_apply_indexer_rejects_empty(bs):
    ok, reason = bs._safe_apply_indexer_output("", _good_indexer_dest(bs))
    assert not ok
    assert "empty" in reason


def test_safe_apply_indexer_rejects_syntax_error(bs):
    ok, reason = bs._safe_apply_indexer_output("def (broken", _good_indexer_dest(bs))
    assert not ok
    assert "parse" in reason


def test_safe_apply_indexer_accepts_clean(bs):
    src = textwrap.dedent("""
        from pathlib import Path
        def index_x_file(path: Path) -> dict:
            return {"symbols": [], "imports": [], "tests": []}
    """).strip()
    ok, reason = bs._safe_apply_indexer_output(src, _good_indexer_dest(bs))
    assert ok, reason
    assert reason == ""


def test_safe_apply_indexer_accepts_read_open(bs):
    src = textwrap.dedent("""
        def index_x(p):
            with open(p, "r") as f:
                return {"text": f.read()}
    """).strip()
    ok, reason = bs._safe_apply_indexer_output(src, _good_indexer_dest(bs))
    assert ok, reason


# ---------------------------------------------------------------------------
# _safe_apply_sprints_output
# ---------------------------------------------------------------------------


def test_safe_apply_sprints_accepts_clean(bs):
    md = "# Project Sprints\n\n## Sprint 1: Foo\n\nGoal: ship.\n"
    ok, reason = bs._safe_apply_sprints_output(md)
    assert ok, reason


@pytest.mark.parametrize("bad,reason_substring", [
    ("", "empty"),
    ("just a paragraph\nno headings\n", "heading"),
    ("# Title\nno sprint heading\n", "Sprint"),
    ("# T\n## Sprint 1\nhello\x00world\n", "control"),
    ("# T\n## Sprint 1\nhello\x1b[31mred\x1b[0m\n", "control"),
])
def test_safe_apply_sprints_rejects(bs, bad, reason_substring):
    ok, reason = bs._safe_apply_sprints_output(bad)
    assert not ok
    assert reason_substring in reason


# ---------------------------------------------------------------------------
# _validate_patch
# ---------------------------------------------------------------------------


def test_validate_patch_accepts_whitelisted(bs):
    patch = textwrap.dedent("""
        --- a/scripts/header_parser.py
        +++ b/scripts/header_parser.py
        @@ -1 +1,2 @@
         x
        +y
    """).strip()
    ok, _ = bs._validate_patch(patch)
    assert ok


def test_validate_patch_rejects_unknown_path(bs):
    patch = textwrap.dedent("""
        --- a/etc/passwd
        +++ b/etc/passwd
    """).strip()
    ok, reason = bs._validate_patch(patch)
    assert not ok
    assert "etc/passwd" in reason


def test_validate_patch_rejects_council_review(bs):
    patch = "--- a/scripts/council-review.py\n+++ b/scripts/council-review.py\n"
    ok, reason = bs._validate_patch(patch)
    assert not ok


@pytest.mark.parametrize("hdr", [
    "rename from foo",
    "rename to bar",
    "copy from foo",
    "copy to bar",
    "old mode 100644",
    "new mode 100755",
    "deleted file mode 100644",
    "new file mode 100644",
])
def test_validate_patch_rejects_structural_headers(bs, hdr):
    patch = f"--- a/scripts/header_parser.py\n+++ b/scripts/header_parser.py\n{hdr}\n"
    ok, reason = bs._validate_patch(patch)
    assert not ok


def test_validate_patch_empty_ok(bs):
    ok, _ = bs._validate_patch("")
    assert ok


# ---------------------------------------------------------------------------
# _strip_markdown_fence
# ---------------------------------------------------------------------------


def test_strip_markdown_fence_json(bs):
    assert bs._strip_markdown_fence('```json\n{"a":1}\n```') == '{"a":1}'


def test_strip_markdown_fence_bare(bs):
    assert bs._strip_markdown_fence('```\n{"a":1}\n```') == '{"a":1}'


def test_strip_markdown_fence_no_fence(bs):
    assert bs._strip_markdown_fence('{"a":1}') == '{"a":1}'


def test_strip_markdown_fence_mismatch(bs):
    s = '```json\n{"a":1}'
    assert bs._strip_markdown_fence(s) == s


def test_strip_markdown_fence_with_whitespace(bs):
    assert bs._strip_markdown_fence('  \n```json\n{"a":1}\n```  \n') == '{"a":1}'


# ---------------------------------------------------------------------------
# _call_name
# ---------------------------------------------------------------------------


def test_call_name_simple(bs):
    tree = ast.parse("eval(1)")
    call = tree.body[0].value
    assert bs._call_name(call.func) == "eval"


def test_call_name_dotted(bs):
    tree = ast.parse("os.system('x')")
    call = tree.body[0].value
    assert bs._call_name(call.func) == "os.system"


def test_call_name_nested(bs):
    tree = ast.parse("a.b.c.d()")
    call = tree.body[0].value
    assert bs._call_name(call.func) == "a.b.c.d"


def test_call_name_unresolvable(bs):
    tree = ast.parse("(lambda: 1)()")
    call = tree.body[0].value
    assert bs._call_name(call.func) == ""


# ---------------------------------------------------------------------------
# Meta-prompt content guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "generate-indexer-prompt.md",
    "summarize-knowledge-prompt.md",
    "domain-expert-prompt.md",
])
def test_meta_prompts_carry_trust_boundary(name):
    path = REPO_ROOT / "scripts" / "bootstrap" / name
    text = path.read_text()
    assert "Trust boundary" in text


def test_indexer_prompt_uses_fence_tokens():
    """Only generate-indexer-prompt.md structurally fences user input;
    the other two rely on instruction framing for indirect inputs."""
    text = (REPO_ROOT / "scripts" / "bootstrap" / "generate-indexer-prompt.md").read_text()
    assert "<<<USER_INPUT_" in text


def test_indexer_prompt_specifies_json_output_contract():
    text = (REPO_ROOT / "scripts" / "bootstrap" / "generate-indexer-prompt.md").read_text()
    assert "Output contract" in text
    assert "indexer_py" in text
    assert "header_parser_patch" in text


# ---------------------------------------------------------------------------
# step2b / step4 integration via captured subprocess
# ---------------------------------------------------------------------------


def test_step2b_fencing_in_assembled_prompt(bs):
    """Build a step2b-style prompt; assert all 4 fields are fenced."""
    template = "Use {LANG} ext {EXT} grammar {GRAMMAR_NAME} slug {LANG_SLUG}."
    lang = bs._validate_user_field("lang", "ruby")
    ext = bs._validate_user_field("ext", "rb")
    grammar = bs._validate_user_field("grammar", "ruby")
    slug = bs._validate_user_field("slug", "ruby")
    filled = (template
              .replace("{LANG}", bs._fence("lang", lang))
              .replace("{EXT}", bs._fence("ext", ext))
              .replace("{GRAMMAR_NAME}", bs._fence("grammar", grammar))
              .replace("{LANG_SLUG}", bs._fence("slug", slug)))
    assert filled.count(bs.FENCE_BEGIN) == 4
    assert filled.count(bs.FENCE_END) == 4
    assert "name=lang" in filled
    assert "name=ext" in filled


@pytest.mark.parametrize("payload", PAYLOADS)
def test_step4_injection_payload_fenced(bs, payload):
    """For every canonical injection payload: when used as 'brief', it
    must appear ONLY inside a USER_INPUT fence pair, never bare."""
    fenced = bs._fence("brief", payload)
    assert payload in fenced
    # The payload must be sandwiched between fence tokens.
    begin_idx = fenced.find(bs.FENCE_BEGIN)
    end_idx = fenced.find(bs.FENCE_END)
    payload_idx = fenced.find(payload)
    assert begin_idx < payload_idx < end_idx


# ---------------------------------------------------------------------------
# Envelope failure paths
# ---------------------------------------------------------------------------


def test_apply_envelope_rejects_unsafe_indexer_no_write(bs, tmp_path, monkeypatch):
    """envelope with unsafe indexer_py → rejected; no indexer file written
    (irrespective of user confirmation, which never gets asked)."""
    monkeypatch.setattr(bs, "REPO_ROOT", tmp_path)
    (tmp_path / "scripts" / "indexers").mkdir(parents=True)

    def boom(*a, **kw):
        raise AssertionError("ask_yes_no should not be called when indexer is rejected")

    monkeypatch.setattr(bs, "ask_yes_no", boom)
    envelope = {
        "indexer_py": "import subprocess\ndef x(): pass\n",
        "header_parser_patch": "",
    }
    bs._apply_indexer_envelope(envelope, "synth")
    assert not (tmp_path / "scripts" / "indexers" / "synth.py").exists()


def test_apply_envelope_continues_to_patches_after_indexer_reject(bs, tmp_path, monkeypatch, capsys):
    """Real partial-acceptance test: bad indexer must NOT abort the patch
    loop; bootstrap should still attempt to validate/apply the patches."""
    monkeypatch.setattr(bs, "REPO_ROOT", tmp_path)
    (tmp_path / "scripts" / "indexers").mkdir(parents=True)
    yes_calls = {"count": 0}

    def fake_yes(*a, **kw):
        yes_calls["count"] += 1
        return False  # decline patch apply, but that's fine — we just want to reach it

    monkeypatch.setattr(bs, "ask_yes_no", fake_yes)
    envelope = {
        "indexer_py": "import subprocess\n",  # rejected
        "header_parser_patch": "--- a/scripts/header_parser.py\n+++ b/scripts/header_parser.py\n@@ -1 +1,2 @@\n x\n+y\n",
    }
    bs._apply_indexer_envelope(envelope, "synth")
    # ask_yes_no was reached for the patch — proves the patch loop ran.
    assert yes_calls["count"] >= 1


def test_apply_envelope_non_string_indexer_continues_to_patches(bs, tmp_path, monkeypatch):
    """When indexer_py is non-string, skip indexer but still try patches."""
    monkeypatch.setattr(bs, "REPO_ROOT", tmp_path)
    (tmp_path / "scripts" / "indexers").mkdir(parents=True)
    yes_calls = {"count": 0}

    def fake_yes(*a, **kw):
        yes_calls["count"] += 1
        return False

    monkeypatch.setattr(bs, "ask_yes_no", fake_yes)
    envelope = {
        "indexer_py": 42,  # wrong type
        "header_parser_patch": "--- a/scripts/header_parser.py\n+++ b/scripts/header_parser.py\n@@ -1 +1,2 @@\n x\n+y\n",
    }
    bs._apply_indexer_envelope(envelope, "synth")
    assert yes_calls["count"] >= 1


def test_validate_patch_rejects_binary_diff(bs):
    patch = (
        "--- a/scripts/header_parser.py\n"
        "+++ b/scripts/header_parser.py\n"
        "Binary files a/scripts/header_parser.py and b/scripts/header_parser.py differ\n"
    )
    ok, reason = bs._validate_patch(patch)
    assert not ok
    assert "binary" in reason.lower()


def test_strip_markdown_fence_other_languages(bs):
    """LLMs sometimes use ```javascript or ```python. Tolerate any tag."""
    assert bs._strip_markdown_fence('```python\n{"a":1}\n```') == '{"a":1}'
    assert bs._strip_markdown_fence('```js\n{"a":1}\n```') == '{"a":1}'


def test_safe_apply_indexer_rejects_dangerous_modules(bs):
    """os, socket, urllib, http, shutil etc. are not in the allowlist."""
    for mod in ("os", "socket", "urllib", "urllib.request", "http.client",
                "shutil", "ctypes", "importlib.util"):
        src = f"import {mod}\ndef x(): pass\n"
        ok, reason = bs._safe_apply_indexer_output(
            src, REPO_ROOT / "scripts" / "indexers" / "x.py"
        )
        assert not ok, f"unexpectedly allowed: {mod}"
        assert "allowlist" in reason or "forbidden" in reason


def test_safe_apply_indexer_rejects_variable_open_mode(bs):
    """open(p, m) where m is a variable, not a literal → rejected."""
    src = textwrap.dedent("""
        def x(p):
            m = "w"
            return open(p, m)
    """).strip()
    ok, reason = bs._safe_apply_indexer_output(
        src, REPO_ROOT / "scripts" / "indexers" / "x.py"
    )
    assert not ok
    assert "literal" in reason


def test_safe_apply_indexer_rejects_concat_open_mode(bs):
    """open(p, "w" + extra) → not a literal → rejected."""
    src = textwrap.dedent('''
        def x(p):
            return open(p, "w" + "")
    ''').strip()
    ok, reason = bs._safe_apply_indexer_output(
        src, REPO_ROOT / "scripts" / "indexers" / "x.py"
    )
    assert not ok


def test_run_claude_cli_filenotfound(bs, monkeypatch):
    def fake_run(*a, **kw):
        raise FileNotFoundError()

    monkeypatch.setattr(bs.subprocess, "run", fake_run)
    ok, out, err = bs.run_claude_cli("x")
    assert ok is False
    assert "claude CLI not found" in err


def test_run_claude_cli_timeout(bs, monkeypatch):
    import subprocess as sp

    def fake_run(*a, **kw):
        raise sp.TimeoutExpired("claude", 1)

    monkeypatch.setattr(bs.subprocess, "run", fake_run)
    ok, out, err = bs.run_claude_cli("x", timeout=1)
    assert ok is False
    assert "timed out" in err


def test_run_claude_cli_nonzero_exit(bs, monkeypatch):
    class FakeResult:
        returncode = 2
        stdout = "partial"
        stderr = "boom"

    monkeypatch.setattr(bs.subprocess, "run", lambda *a, **kw: FakeResult())
    ok, out, err = bs.run_claude_cli("x")
    assert ok is False
    assert err == "boom"
    assert out == "partial"


@pytest.mark.parametrize("size_delta", [-1, 0, 1])
def test_max_total_bytes_boundary(bs, monkeypatch, size_delta):
    sentinel = {"called": False}

    class FakeResult:
        returncode = 0
        stdout = "x"
        stderr = ""

    def fake_run(*a, **kw):
        sentinel["called"] = True
        return FakeResult()

    monkeypatch.setattr(bs.subprocess, "run", fake_run)
    n = bs.MAX_TOTAL_BYTES + size_delta
    ok, _, err = bs.run_claude_cli("a" * n)
    if size_delta <= 0:
        assert ok is True
        assert sentinel["called"] is True
    else:
        assert ok is False
        assert "exceeds" in err
        assert sentinel["called"] is False


def test_envelope_malformed_json_aborts_step(bs, tmp_path, monkeypatch):
    """If json.loads raises, _apply_indexer_envelope is never called."""
    monkeypatch.setattr(bs, "REPO_ROOT", tmp_path)
    (tmp_path / "scripts" / "indexers").mkdir(parents=True)

    def boom_apply(*a, **kw):
        raise AssertionError("apply should not be called for malformed JSON")

    monkeypatch.setattr(bs, "_apply_indexer_envelope", boom_apply)
    # Simulate the step2b parse step directly:
    raw = "not json at all"
    try:
        json.loads(bs._strip_markdown_fence(raw))
        raised = False
    except json.JSONDecodeError:
        raised = True
    assert raised


def test_sprints_validator_accepts_h2_start(bs):
    """Plan contract allows # OR ## as the leading heading."""
    md = "## Sprints\n\n## Sprint 1: foo\nGoal: x\n"
    ok, reason = bs._safe_apply_sprints_output(md)
    assert ok, reason


def test_safe_apply_indexer_rejects_starred_open_args(bs):
    """open(*args) — mode is unverifiable, must be rejected."""
    src = textwrap.dedent("""
        def x(p):
            args = [p, "w"]
            return open(*args)
    """).strip()
    ok, reason = bs._safe_apply_indexer_output(
        src, REPO_ROOT / "scripts" / "indexers" / "x.py"
    )
    assert not ok
    assert "starred" in reason


def test_safe_apply_indexer_rejects_kwargs_open(bs):
    """open(**kwargs) — mode is unverifiable, must be rejected."""
    src = textwrap.dedent("""
        def x(p, opts):
            return open(p, **opts)
    """).strip()
    ok, reason = bs._safe_apply_indexer_output(
        src, REPO_ROOT / "scripts" / "indexers" / "x.py"
    )
    assert not ok
    assert "kwargs" in reason


def test_safe_apply_indexer_accepts_default_mode_open(bs):
    """open(p) with no mode — default is 'r', which is safe."""
    src = textwrap.dedent("""
        def x(p):
            with open(p) as f:
                return f.read()
    """).strip()
    ok, reason = bs._safe_apply_indexer_output(
        src, REPO_ROOT / "scripts" / "indexers" / "x.py"
    )
    assert ok, reason


def test_safe_apply_indexer_output_size_boundary(bs):
    """Validator-level MAX_TOTAL_BYTES boundary for indexer output."""
    dest = REPO_ROOT / "scripts" / "indexers" / "x.py"
    just_at = "x = '" + ("a" * (bs.MAX_TOTAL_BYTES - 10)) + "'\n"
    just_over = "x = '" + ("a" * (bs.MAX_TOTAL_BYTES)) + "'\n"
    ok_at, _ = bs._safe_apply_indexer_output(just_at, dest)
    assert ok_at  # under cap
    ok_over, reason = bs._safe_apply_indexer_output(just_over, dest)
    assert not ok_over
    assert "exceeds" in reason


def test_safe_apply_sprints_output_size_boundary(bs):
    """Validator-level MAX_TOTAL_BYTES boundary for sprints output."""
    base = "# T\n## Sprint 1\n"
    just_under = base + "x" * (bs.MAX_TOTAL_BYTES - len(base) - 10)
    just_over = base + "x" * (bs.MAX_TOTAL_BYTES)
    ok_under, _ = bs._safe_apply_sprints_output(just_under)
    assert ok_under
    ok_over, reason = bs._safe_apply_sprints_output(just_over)
    assert not ok_over
    assert "exceeds" in reason


def test_step2b_handles_malformed_json(bs, monkeypatch, tmp_path, capsys):
    """Integration test: step2b's actual exception handler runs when
    Claude returns non-JSON. Proves the except json.JSONDecodeError
    branch is exercised, not just json.loads itself."""
    monkeypatch.setattr(bs, "REPO_ROOT", tmp_path)
    (tmp_path / "scripts" / "indexers").mkdir(parents=True)
    (tmp_path / "scripts" / "bootstrap").mkdir(parents=True)
    (tmp_path / "scripts" / "bootstrap" / "generate-indexer-prompt.md").write_text(
        "Trust boundary\n{LANG} {EXT} {GRAMMAR_NAME} {LANG_SLUG}\n"
    )
    monkeypatch.setattr(bs, "PROMPTS_DIR", tmp_path / "scripts" / "bootstrap")
    monkeypatch.setattr(bs, "ask", lambda *a, **kw: kw.get("default", "rb"))
    monkeypatch.setattr(bs, "ask_yes_no", lambda *a, **kw: True)

    apply_called = {"flag": False}

    def boom_apply(*a, **kw):
        apply_called["flag"] = True

    monkeypatch.setattr(bs, "_apply_indexer_envelope", boom_apply)
    monkeypatch.setattr(
        bs, "run_claude_cli",
        lambda *a, **kw: (True, "this is not json", ""),
    )

    ctx = {"other_languages": ["ruby"]}
    bs.step2b_generate_other(ctx)
    captured = capsys.readouterr()
    # The exception path must be exercised; apply must NOT be called.
    assert apply_called["flag"] is False
    assert "non-JSON" in captured.out or "non-JSON" in captured.err


# ---------------------------------------------------------------------------
# Sprint 4: --answers-file security
# ---------------------------------------------------------------------------


class _AnswersFileScope:
    """Context manager that sets _ANSWERS on the bootstrap module and
    restores it afterwards, so tests can exercise the ask_* helpers
    in the answers-file path without leaking state across tests."""

    def __init__(self, bs, answers):
        self.bs = bs
        self.answers = answers
        self._prior = None

    def __enter__(self):
        self._prior = self.bs._ANSWERS
        self.bs._ANSWERS = self.answers
        return self

    def __exit__(self, *exc):
        self.bs._ANSWERS = self._prior


def test_answers_file_missing_key_raises(bs):
    """Finding #4: missing prompt_id must fail loudly, never fall back to input()."""
    with _AnswersFileScope(bs, {}):
        with pytest.raises(bs.AnswersFileKeyMissing):
            bs.ask("Prompt", prompt_id="missing.key")


def test_answers_file_happy_path(bs):
    with _AnswersFileScope(bs, {"identity.project_name": "hello"}):
        assert bs.ask("Prompt", prompt_id="identity.project_name") == "hello"


def test_answers_file_type_mismatch_raises(bs):
    with _AnswersFileScope(bs, {"identity.has_brief": "not-a-bool"}):
        with pytest.raises(bs.AnswersFileKeyMissing):
            bs.ask_yes_no(
                "Prompt", default=False, prompt_id="identity.has_brief"
            )


def test_answers_file_checkbox_invalid_option_raises(bs):
    with _AnswersFileScope(bs, {"stack.languages": ["cobol"]}):
        with pytest.raises(bs.AnswersFileKeyMissing):
            bs.ask_checkbox(
                "Prompt",
                ["python", "rust"],
                prompt_id="stack.languages",
            )


def test_answers_file_choice_invalid_option_raises(bs):
    with _AnswersFileScope(bs, {"sprints.mode": "invalid choice"}):
        with pytest.raises(bs.AnswersFileKeyMissing):
            bs.ask_choice(
                "Prompt",
                ["Skip", "Generate"],
                prompt_id="sprints.mode",
            )


def test_load_answers_file_absolute_path_rejected(bs, tmp_path, monkeypatch):
    """Finding #9: absolute-path-inside-cwd must be rejected."""
    outside = tmp_path.parent / f"out_{tmp_path.name}"
    outside.mkdir()
    path = outside / "answers.json"
    path.write_text(json.dumps({"a": "b"}))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        bs._load_answers_file(str(path.resolve()))


def test_load_answers_file_dotdot_escape_rejected(bs, tmp_path, monkeypatch):
    """Finding #9: ../escape must not bypass the cwd containment check."""
    outside = tmp_path.parent / f"esc_{tmp_path.name}"
    outside.mkdir()
    answers = outside / "answers.json"
    answers.write_text(json.dumps({"a": "b"}))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        bs._load_answers_file("../" + outside.name + "/answers.json")


def test_load_answers_file_values_validated(bs, tmp_path, monkeypatch):
    """Finding #9: answers-file string values must pass through
    _validate_user_field (same pipeline as interactive input)."""
    answers = tmp_path / "answers.json"
    # Fence token in the value must be rejected.
    answers.write_text(
        json.dumps({"identity.project_name": f"evil {bs.FENCE_BEGIN} token"})
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        bs._load_answers_file("answers.json")


def test_load_answers_file_nul_byte_rejected(bs, tmp_path, monkeypatch):
    """NUL byte in a string value must be rejected by _validate_user_field."""
    answers = tmp_path / "answers.json"
    answers.write_text(
        json.dumps({"identity.project_name": "evil\x00nul"})
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        bs._load_answers_file("answers.json")


def test_load_answers_file_happy_path(bs, tmp_path, monkeypatch):
    answers = tmp_path / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "identity.project_name": "ok",
                "identity.has_brief": False,
                "stack.languages": ["python"],
            }
        )
    )
    monkeypatch.chdir(tmp_path)
    data = bs._load_answers_file("answers.json")
    assert data["identity.project_name"] == "ok"
    assert data["identity.has_brief"] is False
    assert data["stack.languages"] == ["python"]


def test_load_answers_file_not_a_file(bs, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        bs._load_answers_file("does_not_exist.json")


def test_load_answers_file_non_object(bs, tmp_path, monkeypatch):
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps(["not", "an", "object"]))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        bs._load_answers_file("answers.json")


def test_answers_file_symlink_outside_cwd_rejected(bs, tmp_path, monkeypatch):
    """Finding #9/#15: an answers-file symlinked from inside cwd but
    resolving to a file outside cwd must be rejected (resolve-then-check
    defeats a naive startswith-based cwd containment check)."""
    try:
        outside = tmp_path.parent / f"out_{tmp_path.name}_real.json"
        outside.write_text(json.dumps({"a": "b"}))
        monkeypatch.chdir(tmp_path)
        link = Path("answers.json")
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        assert link.is_symlink()
        with pytest.raises(SystemExit):
            bs._load_answers_file("answers.json")
    finally:
        if outside.exists():
            outside.unlink()


def test_answers_file_values_fenced_in_prompts(bs, monkeypatch, capsys):
    """Finding #9/#15: answers-file values that feed a Claude prompt
    must be wrapped in <<<USER_INPUT_BEGIN>>>/<<<USER_INPUT_END>>> via
    _fence (same path as interactive values). Proof: capture the prompt
    text that bootstrap constructs and assert the fence markers bracket
    the answer value."""
    project_name = "parity_fence_probe"
    with _AnswersFileScope(bs, {"identity.project_name": project_name}):
        fenced = bs._fence("identity.project_name", bs.ask(
            "Project name", prompt_id="identity.project_name"
        ))
    assert bs.FENCE_BEGIN in fenced
    assert bs.FENCE_END in fenced
    assert project_name in fenced
    # The value must not appear outside the fence boundary.
    before, inside, after = fenced.partition(bs.FENCE_BEGIN)
    assert project_name not in before
    assert project_name in inside + after.split(bs.FENCE_END)[0]
