Usage numbers copied (numbers only, no prompt or file content) from a real live
bench on macOS, 2026-11-10. Each row is one JSONL line Claude Code wrote:
[message_index, input_tokens, cache_creation_input_tokens, cache_read_input_tokens,
 output_tokens, model, first_content_block_type].
Claude Code writes one line per content block of a single assistant message and repeats
message.id + message.usage on each. Subagent rows show streaming output growth (5, 5, 268).
tests/test_claude_dedupe.py builds session files from rows.json with fresh timestamps.
