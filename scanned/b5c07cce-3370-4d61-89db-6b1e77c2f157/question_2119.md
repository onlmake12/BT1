# Q2119: Low rpc resource amplification in submit_block

## Question
Can an unprivileged attacker repeatedly send small JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions through a local RPC caller invoking public JSON-RPC methods with crafted parameters to make `submit_block` in `rpc/src/module/miner.rs` amplify CPU, memory, storage, or bandwidth and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `rpc/src/module/miner.rs::submit_block`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
