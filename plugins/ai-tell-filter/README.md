# ai-tell-filter

Strips lexical AI tells from the assistant's final response before it is
delivered. Deterministic, no inference, no extra tokens.

## What it changes

Two edits, both substitutions:

1. **Dashes.** Em dash (`—`), en dash (`–`) and horizontal bar (`―`) used as a
   prose splice become a comma. An en dash between two digits becomes a hyphen,
   so `2020–2024` reads `2020-2024`. A dash that already follows a comma, colon
   or semicolon is dropped rather than doubled.
2. **Tier 1A word swaps** from the `avoid-ai-writing` skill, limited to entries
   with a safe one-for-one replacement: `robust` -> `reliable`, `delve` ->
   `dig`, `seamless` -> `smooth`, `leverage` -> `use`, `comprehensive` ->
   `thorough`, and about twenty more. Inflections are listed explicitly
   (`delved` -> `dug`, not `digged`) and the original capitalisation is carried
   over.

## What it deliberately does not change

Rhythm, structure, sentence length and paragraph shape are left exactly as the
model wrote them. The filter only substitutes: it never deletes a word, never
reflows a line, never re-splits a sentence. The skill's "Never inject"
guardrails are the reason. A mechanical pass that also flattened rhythm would
trade one detectable register for another, and nothing downstream could tell
what the model actually said.

Tier 1A entries whose skill guidance is "rewrite entirely" are left alone
(`tapestry`, `beacon`, `game-changer`, `symphony`): there is no 1:1 swap that
keeps the sentence honest, so a human does it or nobody does. Words with a
common technical sense (`landscape`, `unpack`, `underscore`, `complexities`)
are also out, because the false-positive cost is higher than the tell.

`--` is left alone: in a coding assistant's output it is a CLI flag far more
often than a dash.

## Carve-outs

Never touched:

- fenced code blocks (` ``` ` and `~~~`), including the fence lines
- inline code spans, single or double backtick
- URLs (`https:`, `ftp:`, `file:`, `mailto:`, bare `www.`), with trailing
  sentence punctuation and an unmatched closing bracket handed back to the
  prose so `[docs](https://x.test)` keeps its paren
- absolute, relative and `~` file paths, plus bare filenames with a known
  source/config extension
- blockquote lines (`>`) and markdown table rows (`|`)
- double-quoted material, straight or curly

The filter is idempotent: running it on its own output is a no-op.

## Enabling

```bash
hermes plugins enable ai-tell-filter
```

Bundled but off by default, like every other bundled plugin.

| Env var | Effect |
|---|---|
| `AI_TELL_FILTER_DISABLE=1` | plugin loads, hook is a no-op (fastest raw-vs-filtered comparison) |

Responses over 200,000 characters are passed through untouched; at that size
the text is a pasted artifact, not prose.

## Hook

`transform_llm_output`, fired once per turn after the tool-calling loop and
before delivery. The callback returns a replacement string only when something
actually changed, so a clean turn costs one regex pass. Exceptions inside the
callback are logged and swallowed by the host, and the plugin catches its own
too: a cosmetic pass must never break delivery.

## Tests

```bash
python -m pytest tests/plugins/test_ai_tell_filter_plugin.py
bash scripts/mutation_check_ai_tell_filter.sh python
```

The second script mutates ten production behaviours one at a time and checks
the suite catches each one.

## Attribution

The word list is adapted from the Tier 1A table in the `avoid-ai-writing`
skill by Conor Bronsdon (MIT), vendored in this repo's skills tree.
