# Q3: Low cli boundary divergence in init

## Question
Can an unprivileged attacker enter through a local command-line user invoking supported CKB subcommands with crafted arguments and use local database contents, malformed config files, and supported operator commands to drive `init` in `ckb-bin/src/cli.rs` across a boundary where make generated defaults enable an unsafe resource or performance behavior in normal operation, violating the invariant that supported local CLI and config paths must fail cleanly and not corrupt node state, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `ckb-bin/src/cli.rs::init`
- Entrypoint: a local command-line user invoking supported CKB subcommands with crafted arguments
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
