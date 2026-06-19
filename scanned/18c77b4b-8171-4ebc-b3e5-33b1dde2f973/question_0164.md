# Q164: High cli canonical encoding ambiguity in Config

## Question
Can an unprivileged attacker craft alternate encodings for TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state through a local process starting, stopping, importing, exporting, replaying, or migrating CKB data so `Config` in `util/app-config/src/configs/network_alert.rs` accepts two representations for one security object and make generated defaults enable an unsafe resource or performance behavior in normal operation, violating operator-facing services must not crash or degrade the node through valid local inputs, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/configs/network_alert.rs::Config`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: operator-facing services must not crash or degrade the node through valid local inputs
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
