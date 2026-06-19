# Q286: High cli limit off by one in track_current_process_simple

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options through an operator-facing component processing log, metrics, memory, runtime, or launcher state so `track_current_process_simple` in `util/memory-tracker/src/lib.rs` make generated defaults enable an unsafe resource or performance behavior in normal operation, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/memory-tracker/src/lib.rs::track_current_process_simple`
- Entrypoint: an operator-facing component processing log, metrics, memory, runtime, or launcher state
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
