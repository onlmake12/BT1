# Q160: Low cli cache invalidation failure in max_outbound_peers

## Question
Can an unprivileged attacker use an operator-facing component processing log, metrics, memory, runtime, or launcher state to alternate valid and invalid TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state so `max_outbound_peers` in `util/app-config/src/configs/network.rs` leaves a cache, index, or status flag stale and make generated defaults enable an unsafe resource or performance behavior in normal operation, violating import/export/replay/migration behavior must match production validation and storage invariants, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/app-config/src/configs/network.rs::max_outbound_peers`
- Entrypoint: an operator-facing component processing log, metrics, memory, runtime, or launcher state
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: import/export/replay/migration behavior must match production validation and storage invariants
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
