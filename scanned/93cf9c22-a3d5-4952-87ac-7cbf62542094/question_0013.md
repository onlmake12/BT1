# Q13: High cli cross module inconsistency in run_daemon

## Question
Can an unprivileged attacker use an operator-facing component processing log, metrics, memory, runtime, or launcher state to make `run_daemon` in `ckb-bin/src/lib.rs` return a result that downstream modules interpret differently, where crash the command or node through supported local input before validation or recovery runs, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `ckb-bin/src/lib.rs::run_daemon`
- Entrypoint: an operator-facing component processing log, metrics, memory, runtime, or launcher state
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: crash the command or node through supported local input before validation or recovery runs
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
