# Q326: High cli limit off by one in lib

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state through a local process starting, stopping, importing, exporting, replaying, or migrating CKB data so `lib` in `util/runtime/src/lib.rs` trigger an import/export/replay/migrate path to disagree with normal node validation or storage state, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/runtime/src/lib.rs::lib`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
