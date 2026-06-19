# Q215: Low cli resource amplification in From

## Question
Can an unprivileged attacker repeatedly send small CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options through an operator-facing component processing log, metrics, memory, runtime, or launcher state to make `From` in `util/app-config/src/legacy/store.rs` amplify CPU, memory, storage, or bandwidth and crash the command or node through supported local input before validation or recovery runs, violating import/export/replay/migration behavior must match production validation and storage invariants, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/app-config/src/legacy/store.rs::From`
- Entrypoint: an operator-facing component processing log, metrics, memory, runtime, or launcher state
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: crash the command or node through supported local input before validation or recovery runs
- Invariant to test: import/export/replay/migration behavior must match production validation and storage invariants
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
