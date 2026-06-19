# Q211: Low cli parser precheck gap in from

## Question
Can an unprivileged attacker submit malformed-but-reachable runtime stop timing, spawned process handles, channel pressure, memory tracker samples, and data paths through a local command-line user invoking supported CKB subcommands with crafted arguments so `from` in `util/app-config/src/legacy/mod.rs` performs expensive or unsafe work before validation and crash the command or node through supported local input before validation or recovery runs, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/app-config/src/legacy/mod.rs::from`
- Entrypoint: a local command-line user invoking supported CKB subcommands with crafted arguments
- Attacker controls: runtime stop timing, spawned process handles, channel pressure, memory tracker samples, and data paths
- Exploit idea: crash the command or node through supported local input before validation or recovery runs
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
