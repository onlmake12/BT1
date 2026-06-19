# Q79: Low cli batch interaction bug in sanity_check

## Question
Can an unprivileged attacker batch TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state through a local process starting, stopping, importing, exporting, replaying, or migrating CKB data so `sanity_check` in `ckb-bin/src/subcommand/replay.rs` handles the first item safely but applies incorrect assumptions to later items and trigger an import/export/replay/migrate path to disagree with normal node validation or storage state, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `ckb-bin/src/subcommand/replay.rs::sanity_check`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
