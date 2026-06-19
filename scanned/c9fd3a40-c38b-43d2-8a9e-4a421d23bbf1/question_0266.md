# Q266: High cli limit off by one in log_to_stdout

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options through an operator using default-enabled configuration generated or parsed by the node so `log_to_stdout` in `util/logger-config/src/lib.rs` make generated defaults enable an unsafe resource or performance behavior in normal operation, violating operator-facing services must not crash or degrade the node through valid local inputs, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/logger-config/src/lib.rs::log_to_stdout`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: operator-facing services must not crash or degrade the node through valid local inputs
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
