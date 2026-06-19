# Q170: Low cli limit off by one in Config

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options through an operator-facing component processing log, metrics, memory, runtime, or launcher state so `Config` in `util/app-config/src/configs/notify.rs` cause important performance degradation in a default-enabled operator path with small local input, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/app-config/src/configs/notify.rs::Config`
- Entrypoint: an operator-facing component processing log, metrics, memory, runtime, or launcher state
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: cause important performance degradation in a default-enabled operator path with small local input
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
