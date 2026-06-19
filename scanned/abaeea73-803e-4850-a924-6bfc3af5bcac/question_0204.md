# Q204: High cli resource amplification in from

## Question
Can an unprivileged attacker repeatedly send small CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options through an operator-facing component processing log, metrics, memory, runtime, or launcher state to make `from` in `util/app-config/src/exit_code.rs` amplify CPU, memory, storage, or bandwidth and make generated defaults enable an unsafe resource or performance behavior in normal operation, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/exit_code.rs::from`
- Entrypoint: an operator-facing component processing log, metrics, memory, runtime, or launcher state
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
