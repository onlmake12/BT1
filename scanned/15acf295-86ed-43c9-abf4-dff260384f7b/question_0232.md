# Q232: High cli batch interaction bug in is_enabled

## Question
Can an unprivileged attacker batch TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state through a local command-line user invoking supported CKB subcommands with crafted arguments so `is_enabled` in `util/app-config/src/sentry_config.rs` handles the first item safely but applies incorrect assumptions to later items and cause important performance degradation in a default-enabled operator path with small local input, violating import/export/replay/migration behavior must match production validation and storage invariants, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/sentry_config.rs::is_enabled`
- Entrypoint: a local command-line user invoking supported CKB subcommands with crafted arguments
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: cause important performance degradation in a default-enabled operator path with small local input
- Invariant to test: import/export/replay/migration behavior must match production validation and storage invariants
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
