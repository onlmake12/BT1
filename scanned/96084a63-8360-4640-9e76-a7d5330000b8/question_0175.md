# Q175: High cli parser precheck gap in at_least_100

## Question
Can an unprivileged attacker submit malformed-but-reachable TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state through a local command-line user invoking supported CKB subcommands with crafted arguments so `at_least_100` in `util/app-config/src/configs/notify.rs` performs expensive or unsafe work before validation and make generated defaults enable an unsafe resource or performance behavior in normal operation, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/configs/notify.rs::at_least_100`
- Entrypoint: a local command-line user invoking supported CKB subcommands with crafted arguments
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
