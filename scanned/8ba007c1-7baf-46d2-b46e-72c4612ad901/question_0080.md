# Q80: High cli resource amplification in reset_data

## Question
Can an unprivileged attacker repeatedly send small TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state through an operator using default-enabled configuration generated or parsed by the node to make `reset_data` in `ckb-bin/src/subcommand/reset_data.rs` amplify CPU, memory, storage, or bandwidth and cause important performance degradation in a default-enabled operator path with small local input, violating import/export/replay/migration behavior must match production validation and storage invariants, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `ckb-bin/src/subcommand/reset_data.rs::reset_data`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: cause important performance degradation in a default-enabled operator path with small local input
- Invariant to test: import/export/replay/migration behavior must match production validation and storage invariants
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
