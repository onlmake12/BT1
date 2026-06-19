# Q2363: High rpc boundary divergence in get_block_total_difficulty

## Question
Can an unprivileged attacker enter through a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs and use RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence to drive `get_block_total_difficulty` in `util/light-client-protocol-server/src/components/get_last_state_proof.rs` across a boundary where cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating the invariant that RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/light-client-protocol-server/src/components/get_last_state_proof.rs::get_block_total_difficulty`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
