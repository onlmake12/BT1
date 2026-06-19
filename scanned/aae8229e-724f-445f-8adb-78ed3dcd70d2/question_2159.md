# Q2159: Low rpc limit off by one in get_sys_info

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs so `get_sys_info` in `rpc/src/module/terminal.rs` make RPC/indexer code panic or allocate heavily before validation clamps the request, violating RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `rpc/src/module/terminal.rs::get_sys_info`
- Entrypoint: a miner or wallet integration calling chain, pool, miner, net, debug, or subscription APIs
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: RPC/indexer/helper state must match canonical chain state after reorg, restart, and pagination
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
