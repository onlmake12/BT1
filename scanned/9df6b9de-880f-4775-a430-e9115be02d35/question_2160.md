# Q2160: Low rpc canonical encoding ambiguity in is_expired

## Question
Can an unprivileged attacker craft alternate encodings for indexer state freshness, reorg timing, block-filter requests, and proof target positions through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values so `is_expired` in `rpc/src/module/terminal.rs` accepts two representations for one security object and cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `rpc/src/module/terminal.rs::is_expired`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
