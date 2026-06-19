# Q150: High cli limit off by one in WorkerConfig

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state through an operator using default-enabled configuration generated or parsed by the node so `WorkerConfig` in `util/app-config/src/configs/miner.rs` cause important performance degradation in a default-enabled operator path with small local input, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/configs/miner.rs::WorkerConfig`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: cause important performance degradation in a default-enabled operator path with small local input
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
