# Q182: High cli cache invalidation failure in default_db_password

## Question
Can an unprivileged attacker use an operator using default-enabled configuration generated or parsed by the node to alternate valid and invalid CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options so `default_db_password` in `util/app-config/src/configs/rich_indexer.rs` leaves a cache, index, or status flag stale and make generated defaults enable an unsafe resource or performance behavior in normal operation, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/configs/rich_indexer.rs::default_db_password`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
