# Q27: Low cli boundary divergence in from_setup

## Question
Can an unprivileged attacker enter through an operator-facing component processing log, metrics, memory, runtime, or launcher state and use CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options to drive `from_setup` in `ckb-bin/src/setup_guard.rs` across a boundary where make generated defaults enable an unsafe resource or performance behavior in normal operation, violating the invariant that operator-facing services must not crash or degrade the node through valid local inputs, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `ckb-bin/src/setup_guard.rs::from_setup`
- Entrypoint: an operator-facing component processing log, metrics, memory, runtime, or launcher state
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: operator-facing services must not crash or degrade the node through valid local inputs
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
