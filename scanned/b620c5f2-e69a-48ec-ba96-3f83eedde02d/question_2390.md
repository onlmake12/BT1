# Q2390: Low rpc cross module inconsistency in reply

## Question
Can an unprivileged attacker use a light-client protocol caller requesting proofs and filters across reorg boundaries to make `reply` in `util/light-client-protocol-server/src/prelude.rs` return a result that downstream modules interpret differently, where make RPC/indexer code panic or allocate heavily before validation clamps the request, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `util/light-client-protocol-server/src/prelude.rs::reply`
- Entrypoint: a light-client protocol caller requesting proofs and filters across reorg boundaries
- Attacker controls: indexer state freshness, reorg timing, block-filter requests, and proof target positions
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
