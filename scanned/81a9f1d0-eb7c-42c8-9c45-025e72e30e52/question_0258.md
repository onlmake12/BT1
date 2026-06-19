# Q258: High cli cache invalidation failure in check_indexer_config

## Question
Can an unprivileged attacker use a local process starting, stopping, importing, exporting, replaying, or migrating CKB data to alternate valid and invalid TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state so `check_indexer_config` in `util/launcher/src/lib.rs` leaves a cache, index, or status flag stale and crash the command or node through supported local input before validation or recovery runs, violating import/export/replay/migration behavior must match production validation and storage invariants, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/launcher/src/lib.rs::check_indexer_config`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: crash the command or node through supported local input before validation or recovery runs
- Invariant to test: import/export/replay/migration behavior must match production validation and storage invariants
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
