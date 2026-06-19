# Q2310: Low rpc state transition mismatch in NodeAddress

## Question
Can an unprivileged attacker enter through a local RPC caller invoking public JSON-RPC methods with crafted parameters and sequence JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions so `NodeAddress` in `util/jsonrpc-types/src/net.rs` observes pre-state and post-state from different views, letting the flow cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/net.rs::NodeAddress`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
