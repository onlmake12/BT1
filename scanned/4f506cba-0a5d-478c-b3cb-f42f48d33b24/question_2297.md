# Q2297: High rpc limit off by one in DeploymentState

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs so `DeploymentState` in `util/jsonrpc-types/src/info.rs` return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/jsonrpc-types/src/info.rs::DeploymentState`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
