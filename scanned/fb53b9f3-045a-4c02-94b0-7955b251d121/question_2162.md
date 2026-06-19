# Q2162: Low rpc limit off by one in RpcServer

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values so `RpcServer` in `rpc/src/server.rs` return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `rpc/src/server.rs::RpcServer`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: return stale or inconsistent security-relevant state after reorg, restart, or concurrent indexing
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
