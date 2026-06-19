# Q221: Low cli differential path split in lib

## Question
Can an unprivileged attacker reach `lib` in `util/app-config/src/lib.rs` through two production paths from a local process starting, stopping, importing, exporting, replaying, or migrating CKB data and make one path accept while the other rejects because of TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/app-config/src/lib.rs::lib`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: cause important performance degradation in a default-enabled operator path with small local input
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
