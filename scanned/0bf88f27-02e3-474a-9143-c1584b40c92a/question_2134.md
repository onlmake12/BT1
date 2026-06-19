# Q2134: Low rpc batch interaction bug in build_well_known_lock_scripts

## Question
Can an unprivileged attacker batch JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions through a local RPC caller invoking public JSON-RPC methods with crafted parameters so `build_well_known_lock_scripts` in `rpc/src/module/pool.rs` handles the first item safely but applies incorrect assumptions to later items and amplify storage scans or proof generation with small crafted RPC requests, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `rpc/src/module/pool.rs::build_well_known_lock_scripts`
- Entrypoint: a local RPC caller invoking public JSON-RPC methods with crafted parameters
- Attacker controls: JSON numbers, hashes, block ranges, script/search keys, order flags, limits, cursors, and subscriptions
- Exploit idea: amplify storage scans or proof generation with small crafted RPC requests
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
