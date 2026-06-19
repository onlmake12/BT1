# Q118: Low cli differential path split in ExportTarget

## Question
Can an unprivileged attacker reach `ExportTarget` in `util/app-config/src/args.rs` through two production paths from an operator using default-enabled configuration generated or parsed by the node and make one path accept while the other rejects because of TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/app-config/src/args.rs::ExportTarget`
- Entrypoint: an operator using default-enabled configuration generated or parsed by the node
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
