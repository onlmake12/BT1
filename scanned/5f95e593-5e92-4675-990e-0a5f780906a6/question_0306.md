# Q306: High cli resource amplification in check_exporter_name

## Question
Can an unprivileged attacker repeatedly send small TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state through an operator using default-enabled configuration generated or parsed by the node to make `check_exporter_name` in `util/metrics-service/src/lib.rs` amplify CPU, memory, storage, or bandwidth and cause important performance degradation in a default-enabled operator path with small local input, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/metrics-service/src/lib.rs::check_exporter_name`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: cause important performance degradation in a default-enabled operator path with small local input
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
