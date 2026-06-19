# Q44: High cli canonical encoding ambiguity in init

## Question
Can an unprivileged attacker craft alternate encodings for TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state through a local command-line user invoking supported CKB subcommands with crafted arguments so `init` in `ckb-bin/src/subcommand/init.rs` accepts two representations for one security object and crash the command or node through supported local input before validation or recovery runs, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `ckb-bin/src/subcommand/init.rs::init`
- Entrypoint: a local command-line user invoking supported CKB subcommands with crafted arguments
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: crash the command or node through supported local input before validation or recovery runs
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
