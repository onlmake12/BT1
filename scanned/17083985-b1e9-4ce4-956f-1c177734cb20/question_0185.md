# Q185: Low cli canonical encoding ambiguity in experiment_enable

## Question
Can an unprivileged attacker craft alternate encodings for TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state through a local process starting, stopping, importing, exporting, replaying, or migrating CKB data so `experiment_enable` in `util/app-config/src/configs/rpc.rs` accepts two representations for one security object and trigger an import/export/replay/migrate path to disagree with normal node validation or storage state, violating operator-facing services must not crash or degrade the node through valid local inputs, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/app-config/src/configs/rpc.rs::experiment_enable`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: TOML config values, resource templates, default directories, sentry/log/metrics settings, and process state
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: operator-facing services must not crash or degrade the node through valid local inputs
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
