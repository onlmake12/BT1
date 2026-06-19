# Q196: Low cli boundary divergence in TxPoolConfig

## Question
Can an unprivileged attacker enter through a local process starting, stopping, importing, exporting, replaying, or migrating CKB data and use local database contents, malformed config files, and supported operator commands to drive `TxPoolConfig` in `util/app-config/src/configs/tx_pool.rs` across a boundary where trigger an import/export/replay/migrate path to disagree with normal node validation or storage state, violating the invariant that supported local CLI and config paths must fail cleanly and not corrupt node state, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/app-config/src/configs/tx_pool.rs::TxPoolConfig`
- Entrypoint: a local process starting, stopping, importing, exporting, replaying, or migrating CKB data
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
