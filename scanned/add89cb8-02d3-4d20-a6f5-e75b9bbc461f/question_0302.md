# Q302: Low cli differential path split in Exporter

## Question
Can an unprivileged attacker reach `Exporter` in `util/metrics-config/src/lib.rs` through two production paths from an operator-facing component processing log, metrics, memory, runtime, or launcher state and make one path accept while the other rejects because of TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state, violating operator-facing services must not crash or degrade the node through valid local inputs, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/metrics-config/src/lib.rs::Exporter`
- Entrypoint: an operator-facing component processing log, metrics, memory, runtime, or launcher state
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: operator-facing services must not crash or degrade the node through valid local inputs
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
