# Q29: High cli limit off by one in kill_process

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for local database contents, malformed config files, and supported operator commands through a local command-line user invoking supported CKB subcommands with crafted arguments so `kill_process` in `ckb-bin/src/subcommand/daemon.rs` make generated defaults enable an unsafe resource or performance behavior in normal operation, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `ckb-bin/src/subcommand/daemon.rs::kill_process`
- Entrypoint: a local command-line user invoking supported CKB subcommands with crafted arguments
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
