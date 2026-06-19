# Q154: Low cli canonical encoding ambiguity in configs

## Question
Can an unprivileged attacker craft alternate encodings for TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state through a local process starting, stopping, importing, exporting, replaying, or migrating CKB data so `configs` in `util/app-config/src/configs/mod.rs` accepts two representations for one security object and make generated defaults enable an unsafe resource or performance behavior in normal operation, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/app-config/src/configs/mod.rs::configs`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
