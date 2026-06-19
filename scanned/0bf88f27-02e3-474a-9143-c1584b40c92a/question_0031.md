# Q31: High cli resource amplification in kill_process

## Question
Can an unprivileged attacker repeatedly send small runtime stop timing, spawned process handles, channel pressure, memory tracker samples, and data paths through a local process starting, stopping, importing, exporting, replaying, or migrating CKB data to make `kill_process` in `ckb-bin/src/subcommand/daemon.rs` amplify CPU, memory, storage, or bandwidth and crash the command or node through supported local input before validation or recovery runs, violating operator-facing services must not crash or degrade the node through valid local inputs, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `ckb-bin/src/subcommand/daemon.rs::kill_process`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: runtime stop timing, spawned process handles, channel pressure, memory tracker samples, and data paths
- Exploit idea: crash the command or node through supported local input before validation or recovery runs
- Invariant to test: operator-facing services must not crash or degrade the node through valid local inputs
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
