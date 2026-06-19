# Q323: Low cli limit off by one in lib

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state through a local command-line user invoking supported CKB subcommands with crafted arguments so `lib` in `util/runtime/src/lib.rs` make generated defaults enable an unsafe resource or performance behavior in normal operation, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/runtime/src/lib.rs::lib`
- Entrypoint: a local command-line user invoking supported CKB subcommands with crafted arguments
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
