# Q2342: High rpc resource amplification in TerminalPoolInfo

## Question
Can an unprivileged attacker repeatedly send small RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs to make `TerminalPoolInfo` in `util/jsonrpc-types/src/terminal.rs` amplify CPU, memory, storage, or bandwidth and cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/terminal.rs::TerminalPoolInfo`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
