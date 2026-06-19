# Q115: Low cli resource amplification in path_specified_or_else

## Question
Can an unprivileged attacker repeatedly send small CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options through an operator using default-enabled configuration generated or parsed by the node to make `path_specified_or_else` in `util/app-config/src/app_config.rs` amplify CPU, memory, storage, or bandwidth and crash the command or node through supported local input before validation or recovery runs, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/app-config/src/app_config.rs::path_specified_or_else`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: CLI flags, file paths, chain spec choice, import/export ranges, replay targets, and migration options
- Exploit idea: crash the command or node through supported local input before validation or recovery runs
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
