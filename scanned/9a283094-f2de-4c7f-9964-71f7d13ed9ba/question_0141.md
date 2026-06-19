# Q141: Low cli canonical encoding ambiguity in from

## Question
Can an unprivileged attacker craft alternate encodings for TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state through an operator using default-enabled configuration generated or parsed by the node so `from` in `util/app-config/src/configs/indexer.rs` accepts two representations for one security object and make generated defaults enable an unsafe resource or performance behavior in normal operation, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/app-config/src/configs/indexer.rs::from`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
