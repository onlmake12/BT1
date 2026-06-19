# Q2412: Low rpc parser precheck gap in query_uncle_id_list_by_block_id

## Question
Can an unprivileged attacker submit malformed-but-reachable JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions through a light-client protocol caller requesting proofs and filters across reorg boundaries so `query_uncle_id_list_by_block_id` in `util/rich-indexer/src/indexer/remove.rs` performs expensive or unsafe work before validation and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/rich-indexer/src/indexer/remove.rs::query_uncle_id_list_by_block_id`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
