# Q174: High cli limit off by one in Config

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state through an operator using default-enabled configuration generated or parsed by the node so `Config` in `util/app-config/src/configs/notify.rs` trigger an import/export/replay/migrate path to disagree with normal node validation or storage state, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/configs/notify.rs::Config`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
