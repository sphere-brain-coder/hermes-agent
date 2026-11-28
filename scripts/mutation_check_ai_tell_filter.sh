#!/usr/bin/env bash
# Mutation check for the ai-tell-filter test suite.
#
# Each mutation breaks one production behaviour the tests claim to
# protect. A mutation that leaves the suite green means the behaviour is
# untested, which is the thing this script exists to catch.
#
# Usage: bash scripts/mutation_check_ai_tell_filter.sh <python>
set -u

PY="${1:-python3}"
SRC="plugins/ai-tell-filter/tells.py"
INIT="plugins/ai-tell-filter/__init__.py"
TEST="tests/plugins/test_ai_tell_filter_plugin.py"

cp "$SRC" /tmp/.tells.orig
cp "$INIT" /tmp/.init.orig
restore() { cp /tmp/.tells.orig "$SRC"; cp /tmp/.init.orig "$INIT"; }
trap restore EXIT

fail=0

run_mutation() {
  local name="$1"; shift
  restore
  "$@"
  if "$PY" -m pytest "$TEST" -q >/tmp/.mut_out 2>&1; then
    echo "SURVIVED  $name   <-- untested behaviour"
    fail=1
  else
    echo "caught    $name   ($(grep -cE '^(FAILED|ERROR)' /tmp/.mut_out) failing)"
  fi
}

mut_sed() { "$PY" - "$1" "$2" "$3" <<'EOF'
import sys, pathlib
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path); t = p.read_text()
assert old in t, f"mutation anchor missing: {old!r}"
p.write_text(t.replace(old, new, 1))
EOF
}

# 1. Fences stop being honoured: code blocks get rewritten.
run_mutation "code fences not skipped" \
  mut_sed "$SRC" "        if fence_char:" "        if False:"

# 2. Blockquotes stop being skipped.
run_mutation "blockquote lines not skipped" \
  mut_sed "$SRC" "            or _BLOCKQUOTE_RE.match(line)" "            or False"

# 3. Protected spans stop being masked: inline code, URLs, paths, quotes.
run_mutation "protected spans not masked" \
  mut_sed "$SRC" "    for pattern, trim_tail in _PROTECTED_SPANS:" "    for pattern, trim_tail in ():"

# 4. Case is dropped from the replacement.
run_mutation "capitalisation not carried" \
  mut_sed "$SRC" "    if original[:1].isupper():" "    if False:"

# 5. Dash becomes a hyphen everywhere, ignoring the number-range rule.
run_mutation "dash replacement char wrong" \
  mut_sed "$SRC" '        return ", "' '        return " - "'

# 6. List-item typography carve-out removed.
run_mutation "list typography not carved out" \
  mut_sed "$SRC" "        if _is_list_typography(before):" "        if False:"

# 7. Double punctuation guard removed.
run_mutation "comma+dash stacking guard removed" \
  mut_sed "$SRC" '        if before.rstrip()[-1:] in ",;:":' "        if False:"

# 8. Word-boundary anchors dropped: substring matches fire.
run_mutation "word boundaries dropped" \
  mut_sed "$SRC" '    r"\b(?:%s)\b" % "|".join(' '    r"(?:%s)" % "|".join('

# 9. Hook returns a value even when nothing changed.
run_mutation "hook replaces unchanged responses" \
  mut_sed "$INIT" "    if cleaned == response_text:" "    if False:"

# 10. Kill switch ignored.
run_mutation "kill switch ignored" \
  mut_sed "$INIT" "    if _disabled():" "    if False:"

# --- carve-out leaks found in review round 1 -------------------------------

# 11. Bare relative paths stop being protected.
run_mutation "bare relative paths not protected" \
  mut_sed "$SRC" '    (re.compile(r"(?<![\w.\-/])(?:[\w.\-]+/)+[\w.\-]*"), True),' ""

# 12. Indented code blocks treated as prose again.
run_mutation "indented code not protected" \
  mut_sed "$SRC" "        if _INDENTED_CODE_RE.match(line):" "        if False:"

# 13. Fence length ignored: a shorter inner fence closes a longer block.
run_mutation "fence length ignored" \
  mut_sed "$SRC" "                    and len(run) >= fence_len" "                    and True"

# 14. Fence character ignored: ``` closes a ~~~ block.
run_mutation "fence char ignored" \
  mut_sed "$SRC" "                    run[0] == fence_char" "                    True"

# 15. GFM tables without leading pipes unprotected again.
run_mutation "pipe-only table rows not protected" \
  mut_sed "$SRC" "            i in table_rows" "            False"

# 16. Table detection loses its delimiter-row requirement, so any
#     pipe-bearing prose line would be skipped (over-protection).
run_mutation "table delimiter requirement dropped" \
  mut_sed "$SRC" "            and _TABLE_DELIM_RE.match(lines[i + 1])" "            and True"

# 17. List continuation misread as indented code (over-protection).
run_mutation "list continuation treated as code" \
  mut_sed "$SRC" "            if in_indented_code or (prev_blank and not in_list):" \
                 "            if in_indented_code or prev_blank:"

restore
echo
if [ "$fail" -eq 0 ]; then
  echo "all mutations caught"
else
  echo "at least one mutation survived"
fi
exit "$fail"
