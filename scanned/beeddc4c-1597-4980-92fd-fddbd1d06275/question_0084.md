# Q84: High cli restart reorg persistence in reset_data

## Question
Can an unprivileged attacker shape local database contents, malformed config files, and supported operator commands through an operator using default-enabled configuration generated or parsed by the node, then force normal restart, reorg, retry, or replay handling so `reset_data` in `ckb-bin/src/subcommand/reset_data.rs` persists inconsistent state and crash the command or node through supported local input before validation or recovery runs, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `ckb-bin/src/subcommand/reset_data.rs::reset_data`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: crash the command or node through supported local input before validation or recovery runs
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
