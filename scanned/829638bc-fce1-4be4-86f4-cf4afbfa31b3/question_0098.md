# Q98: Low cli parser precheck gap in raw

## Question
Can an unprivileged attacker submit malformed-but-reachable CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options through an operator using default-enabled configuration generated or parsed by the node so `raw` in `resource/src/lib.rs` performs expensive or unsafe work before validation and crash the command or node through supported local input before validation or recovery runs, violating import/export/replay/migration behavior must match production validation and storage invariants, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `resource/src/lib.rs::raw`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: crash the command or node through supported local input before validation or recovery runs
- Invariant to test: import/export/replay/migration behavior must match production validation and storage invariants
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
