# Q147: High cli cache invalidation failure in DummyConfig

## Question
Can an unprivileged attacker use an operator-facing component processing log, metrics, memory, runtime, or launcher state to alternate valid and invalid TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state so `DummyConfig` in `util/app-config/src/configs/miner.rs` leaves a cache, index, or status flag stale and trigger an import/export/replay/migrate path to disagree with normal node validation or storage state, violating import/export/replay/migration behavior must match production validation and storage invariants, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/configs/miner.rs::DummyConfig`
- Entrypoint: an operator-facing component processing log, metrics, memory, runtime, or launcher state
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: import/export/replay/migration behavior must match production validation and storage invariants
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
