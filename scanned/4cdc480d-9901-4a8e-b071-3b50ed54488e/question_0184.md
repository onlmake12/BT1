# Q184: High cli differential path split in fmt

## Question
Can an unprivileged attacker reach `fmt` in `util/app-config/src/configs/rich_indexer.rs` through two production paths from an operator using default-enabled configuration generated or parsed by the node and make one path accept while the other rejects because of TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state, violating import/export/replay/migration behavior must match production validation and storage invariants, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/configs/rich_indexer.rs::fmt`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: crash the command or node through supported local input before validation or recovery runs
- Invariant to test: import/export/replay/migration behavior must match production validation and storage invariants
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
