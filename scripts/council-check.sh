#!/usr/bin/env bash
# File: scripts/council-check.sh
# Purpose: Pre-flight check that the Council of Experts review system is fully configured (keys, SDK, Codex login, config).
#
# Role:
#   Verifies GOOGLE_API_KEY (env or ~/.zprofile), google-genai SDK, Codex CLI install
#   and login, live connectivity tests to Gemini and Codex, council-config.json presence,
#   council-review.py executability, and git repo context. Exits non-zero with FAIL count.
#
# Last updated: Sprint 123 (2026-04-13) -- initial header block
#
# council-check.sh — Pre-flight check for the council review system.
# Run before council-review.py to verify everything is configured correctly.

set -euo pipefail

PASS=0
FAIL=0
WARN=0

ok()   { echo "  [OK]   $1"; PASS=$((PASS + 1)); }
warn() { echo "  [WARN] $1"; WARN=$((WARN + 1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
info() { echo "         $1"; }

echo ""
echo "=== Council Review Pre-flight Check ==="
echo ""

# Helper: get effective key value (env or ~/.zprofile)
get_key() {
    local name="$1"
    local val="${!name:-}"
    if [ -z "$val" ] && [ -f ~/.zprofile ]; then
        val=$(grep "^export ${name}=" ~/.zprofile 2>/dev/null | head -1 | sed "s/export ${name}=//; s/\"//g; s/'//g")
    fi
    echo "$val"
}

# 1. Google API key (required: Performance, Cost, UX experts + all Google fallbacks)
GOOGLE_KEY=$(get_key GOOGLE_API_KEY)
if [ -n "${GOOGLE_API_KEY:-}" ]; then
    ok "GOOGLE_API_KEY in environment (${#GOOGLE_API_KEY} chars)"
elif [ -n "$GOOGLE_KEY" ]; then
    ok "GOOGLE_API_KEY in ~/.zprofile — auto-sourced by council-review.py"
else
    fail "GOOGLE_API_KEY not found in environment or ~/.zprofile"
    info "Add to ~/.zprofile:  export GOOGLE_API_KEY=\"your-google-key\""
    info "Get key at: https://aistudio.google.com/apikey"
fi

# 2. google-genai Python SDK
if python3 -c "from google import genai" 2>/dev/null; then
    ok "google-genai Python SDK installed"
else
    fail "google-genai SDK not installed"
    info "Install with: pip install google-genai"
fi

# 3. Codex CLI installed
if command -v codex &>/dev/null; then
    CODEX_VER=$(codex --version 2>/dev/null | head -1)
    ok "Codex CLI installed: $CODEX_VER"
else
    fail "Codex CLI not installed"
    info "Install with: npm install -g @openai/codex"
fi

# 4. Codex account login (uses stored session, not API key)
if command -v codex &>/dev/null; then
    if codex login status &>/dev/null; then
        LOGIN_STATUS=$(codex login status 2>&1 || true)
        ok "Codex: logged in ($LOGIN_STATUS)"
    else
        fail "Codex: not logged in"
        info "Run this in your terminal:  codex login"
        info "This opens a browser for OpenAI account authentication."
        info "After login, re-run: ./scripts/council-check.sh"
    fi
fi

# 5. Google API connectivity test
if [ -n "$GOOGLE_KEY" ]; then
    RESULT=$(python3 -c "
from google import genai
client = genai.Client(api_key='$GOOGLE_KEY')
resp = client.models.generate_content(
    model='gemini-2.5-flash',
    contents='Reply with: OK',
    config={'max_output_tokens': 100, 'temperature': 0},
)
print(resp.text.strip() if resp.text else 'EMPTY')
" 2>&1)
    if echo "$RESULT" | grep -q "OK"; then
        ok "Google Gemini API: connected (gemini-2.5-flash responded)"
    else
        fail "Google Gemini API call failed: $RESULT"
    fi
else
    warn "Skipping Google API connectivity test (key not available)"
fi

# 6. Codex connectivity test (quick non-interactive exec) — only if logged in
if command -v codex &>/dev/null && codex login status &>/dev/null; then
    CODEX_OUT=$(timeout 45 codex exec --full-auto "Reply with exactly the text: COUNCIL_OK" 2>&1 || true)
    if echo "$CODEX_OUT" | grep -q "COUNCIL_OK"; then
        ok "Codex API: connected and responding"
    elif echo "$CODEX_OUT" | grep -qi "401\|unauthorized\|authentication"; then
        fail "Codex API: authentication error — run 'codex login'"
    else
        warn "Codex API: response did not contain COUNCIL_OK"
        info "Last output: $(echo "$CODEX_OUT" | tail -3)"
    fi
fi

# 7. council-config.json
CONFIG="$(dirname "$0")/council-config.json"
if [ -f "$CONFIG" ]; then
    VERSION=$(python3 -c "import json; c=json.load(open('$CONFIG')); print(c.get('version', 'unknown'))")
    ok "council-config.json found (version $VERSION)"
else
    fail "council-config.json not found at $CONFIG"
fi

# 8. council-review.py is executable
SCRIPT="$(dirname "$0")/council-review.py"
if [ -x "$SCRIPT" ]; then
    ok "council-review.py is executable"
else
    fail "council-review.py is not executable"
    info "Fix with: chmod +x $SCRIPT"
fi

# 9. Git repo
if git rev-parse --show-toplevel &>/dev/null; then
    ok "Inside a git repository"
else
    fail "Not inside a git repository (council-review.py requires git)"
fi

echo ""
echo "=== Summary: $PASS passed, $WARN warnings, $FAIL failed ==="

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "Fix the FAIL items above before running council-review.py."
    echo ""
    echo "Quick setup:"
    echo "  Google key — add to ~/.zprofile:"
    echo "    export GOOGLE_API_KEY=\"your-key\"   # https://aistudio.google.com/apikey"
    echo ""
    echo "  Codex login — run once in your terminal:"
    echo "    codex login                         # opens browser for OpenAI account auth"
    echo ""
    echo "  Then re-run: ./scripts/council-check.sh"
    exit "$FAIL"
fi

echo ""
echo "Council review is ready. Example usage:"
echo "  ./scripts/council-review.py --allow-external-code-review plan <N> \"<title>\""
echo "  ./scripts/council-review.py --allow-external-code-review code <N> \"<title>\""
echo ""
