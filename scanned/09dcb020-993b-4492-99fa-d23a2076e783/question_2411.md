# Q2411: Low rpc limit off by one in query_tx_id_list_by_block_id

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions through a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs so `query_tx_id_list_by_block_id` in `util/rich-indexer/src/indexer/remove.rs` make RPC/indexer code panic or allocate heavily before validation clamps the request, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/rich-indexer/src/indexer/remove.rs::query_tx_id_list_by_block_id`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
