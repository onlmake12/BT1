# Q85: High cli parser precheck gap in reset_data

## Question
Can an unprivileged attacker submit malformed-but-reachable local database contents, malformed config files, and supported operator commands through a local process starting, stopping, importing, exporting, replaying, or migrating CKB data so `reset_data` in `ckb-bin/src/subcommand/reset_data.rs` performs expensive or unsafe work before validation and crash the command or node through supported local input before validation or recovery runs, violating operator-facing services must not crash or degrade the node through valid local inputs, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `ckb-bin/src/subcommand/reset_data.rs::reset_data`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: crash the command or node through supported local input before validation or recovery runs
- Invariant to test: operator-facing services must not crash or degrade the node through valid local inputs
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
