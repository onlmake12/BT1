# Q2406: High rpc state transition mismatch in set_init_tip

## Question
Can an unprivileged attacker enter through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values and sequence indexer state freshness, reorg timing, block-filter requests, and proof target positions so `set_init_tip` in `util/rich-indexer/src/indexer/mod.rs` observes pre-state and post-state from different views, letting the flow cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay, violating RPC submission paths must not bypass consensus, tx-pool, or block-template validation, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `util/rich-indexer/src/indexer/mod.rs::set_init_tip`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: cause locally submitted blocks/transactions to enter a path with weaker validation than P2P relay
- Invariant to test: RPC submission paths must not bypass consensus, tx-pool, or block-template validation
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
