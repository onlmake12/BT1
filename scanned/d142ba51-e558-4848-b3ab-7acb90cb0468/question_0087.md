# Q87: High cli differential path split in calc_rpc_threads_num

## Question
Can an unprivileged attacker reach `calc_rpc_threads_num` in `ckb-bin/src/subcommand/run.rs` through two production paths from a local command-line user invoking supported CKB subcommands with crafted arguments and make one path accept while the other rejects because of local database contents, malformed config files, and supported operator commands, violating supported local CLI and config paths must fail cleanly and not corrupt node state, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `ckb-bin/src/subcommand/run.rs::calc_rpc_threads_num`
- Entrypoint: a local command-line user invoking supported CKB subcommands with crafted arguments
- Attacker controls: local database contents, malformed config files, and supported operator commands
- Exploit idea: trigger an import/export/replay/migrate path to disagree with normal node validation or storage state
- Invariant to test: supported local CLI and config paths must fail cleanly and not corrupt node state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run the supported CLI/config path in a temp directory with crafted local input; assert clean error, no state corruption, and bounded runtime.
