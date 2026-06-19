# Q5: High cli cache invalidation failure in deadlock_detection

## Question
Can an unprivileged attacker use a local command-line user invoking supported CKB subcommands with crafted arguments to alternate valid and invalid local database contents, malformed config files, and supported operator commands so `deadlock_detection` in `ckb-bin/src/helper.rs` leaves a cache, index, or status flag stale and make generated defaults enable an unsafe resource or performance behavior in normal operation, violating operator-facing services must not crash or degrade the node through valid local inputs, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `ckb-bin/src/helper.rs::deadlock_detection`
- Entrypoint: a local command-line user invoking supported CKB subcommands with crafted arguments
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: operator-facing services must not crash or degrade the node through valid local inputs
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
