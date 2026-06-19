# Q2077: Note rpc resource amplification in lib

## Question
Can an unprivileged attacker repeatedly send small block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values to make `lib` in `block-filter/src/lib.rs` amplify CPU, memory, storage, or bandwidth and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Note (0 - 500 points). Any local RPC API crash?

## Target
- File/function: `block-filter/src/lib.rs::lib`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Note (0 - 500 points). Any local RPC API crash
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
