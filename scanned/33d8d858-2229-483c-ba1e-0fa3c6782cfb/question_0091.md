# Q91: Low cli differential path split in build

## Question
Can an unprivileged attacker reach `build` in `ckb-bin/src/subcommand/stats.rs` through two production paths from an operator-facing component processing log, metrics, memory, runtime, or launcher state and make one path accept while the other rejects because of CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `ckb-bin/src/subcommand/stats.rs::build`
- Entrypoint: an operator-facing component processing log, metrics, memory, runtime, or launcher state
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: cause important performance degradation in a default-enabled operator path with small local input
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
