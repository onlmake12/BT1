# Q166: High cli differential path split in Config

## Question
Can an unprivileged attacker reach `Config` in `util/app-config/src/configs/network_alert.rs` through two production paths from a local command-line user invoking supported CKB subcommands with crafted arguments and make one path accept while the other rejects because of TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/configs/network_alert.rs::Config`
- Entrypoint: a local command-line user invoking supported CKB subcommands with crafted arguments
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
