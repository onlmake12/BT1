# Q2338: Low rpc batch interaction bug in DiskUsage

## Question
Can an unprivileged attacker batch block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through a light-client protocol caller requesting proofs and filters across reorg boundaries so `DiskUsage` in `util/jsonrpc-types/src/terminal.rs` handles the first item safely but applies incorrect assumptions to later items and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/terminal.rs::DiskUsage`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
