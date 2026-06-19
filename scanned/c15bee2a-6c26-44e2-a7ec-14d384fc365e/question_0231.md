# Q231: High cli cache invalidation failure in is_enabled

## Question
Can an unprivileged attacker use a local process starting, stopping, importing, exporting, replaying, or migrating CKB data to alternate valid and invalid CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options so `is_enabled` in `util/app-config/src/sentry_config.rs` leaves a cache, index, or status flag stale and trigger an import/export/replay/migrate path to disagree with normal node validation or storage state, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/sentry_config.rs::is_enabled`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
