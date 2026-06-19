# Q2152: Low rpc canonical encoding ambiguity in SubscriptionRpc

## Question
Can an unprivileged attacker craft alternate encodings for RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through a local RPC caller invoking public JSON-RPC methods with crafted parameters so `SubscriptionRpc` in `rpc/src/module/subscription.rs` accepts two representations for one security object and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `rpc/src/module/subscription.rs::SubscriptionRpc`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
