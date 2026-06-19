# Q111: Low cli canonical encoding ambiguity in canonicalize_data_dir

## Question
Can an unprivileged attacker craft alternate encodings for local database contents, malformed config files, and supported operator commands through an operator-facing component processing log, metrics, memory, runtime, or launcher state so `canonicalize_data_dir` in `util/app-config/src/app_config.rs` accepts two representations for one security object and trigger an import/export/replay/migrate path to disagree with normal node validation or storage state, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/app-config/src/app_config.rs::canonicalize_data_dir`
- Entrypoint: an operator-facing component processing log, metrics, memory, runtime, or launcher state
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
