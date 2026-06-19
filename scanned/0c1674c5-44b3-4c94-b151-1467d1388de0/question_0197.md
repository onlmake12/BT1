# Q197: High cli differential path split in TxPoolConfig

## Question
Can an unprivileged attacker reach `TxPoolConfig` in `util/app-config/src/configs/tx_pool.rs` through two production paths from a local command-line user invoking supported CKB subcommands with crafted arguments and make one path accept while the other rejects because of local database contents, malformed config files, and supported operator commands, violating default-enabled configuration must preserve security, performance, and protocol assumptions, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/app-config/src/configs/tx_pool.rs::TxPoolConfig`
- Entrypoint: a local command-line user invoking supported CKB subcommands with crafted arguments
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: make generated defaults enable an unsafe resource or performance behavior in normal operation
- Invariant to test: default-enabled configuration must preserve security, performance, and protocol assumptions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
