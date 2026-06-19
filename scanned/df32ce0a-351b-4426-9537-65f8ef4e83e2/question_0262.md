# Q262: High cli limit off by one in start_block_filter

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for local database contents, malformed config files, and supported operator commands through an operator-facing component processing log, metrics, memory, runtime, or launcher state so `start_block_filter` in `util/launcher/src/lib.rs` cause important performance degradation in a default-enabled operator path with small local input, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/launcher/src/lib.rs::start_block_filter`
- Entrypoint: an operator-facing component processing log, metrics, memory, runtime, or launcher state
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: cause important performance degradation in a default-enabled operator path with small local input
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
