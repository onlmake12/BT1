# Q2313: Low rpc batch interaction bug in RemoteNodeProtocol

## Question
Can an unprivileged attacker batch RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence through a light-client protocol caller requesting proofs and filters across reorg boundaries so `RemoteNodeProtocol` in `util/jsonrpc-types/src/net.rs` handles the first item safely but applies incorrect assumptions to later items and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating local RPC APIs must validate all caller-controlled parameters and fail without process crash, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/jsonrpc-types/src/net.rs::RemoteNodeProtocol`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: RPC batch size, malformed hex/bytes, pagination direction, filter shape, and repeated polling cadence
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: local RPC APIs must validate all caller-controlled parameters and fail without process crash
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
