# Q86: Low cli limit off by one in reset_data

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for local database contents, malformed config files, and supported operator commands through an operator-facing component processing log, metrics, memory, runtime, or launcher state so `reset_data` in `ckb-bin/src/subcommand/reset_data.rs` trigger an import/export/replay/migrate path to disagree with normal node validation or storage state, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `ckb-bin/src/subcommand/reset_data.rs::reset_data`
- Entrypoint: an operator-facing component processing log, metrics, memory, runtime, or launcher state
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
