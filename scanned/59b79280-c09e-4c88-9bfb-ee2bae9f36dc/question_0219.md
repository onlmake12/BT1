# Q219: Low cli limit off by one in default_expiry_hours

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for local database contents, malformed config files, and supported operator commands through an operator-facing component processing log, metrics, memory, runtime, or launcher state so `default_expiry_hours` in `util/app-config/src/legacy/tx_pool.rs` cause important performance degradation in a default-enabled operator path with small local input, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/app-config/src/legacy/tx_pool.rs::default_expiry_hours`
- Entrypoint: an operator-facing component processing log, metrics, memory, runtime, or launcher state
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: cause important performance degradation in a default-enabled operator path with small local input
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
