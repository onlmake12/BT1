# Q2104: Low rpc resource amplification in calculate_dao_maximum_withdraw

## Question
Can an unprivileged attacker repeatedly send small block/template parameters, transaction payloads, fee-rate values, and debug/experiment options through an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values to make `calculate_dao_maximum_withdraw` in `rpc/src/module/experiment.rs` amplify CPU, memory, storage, or bandwidth and make RPC/indexer code panic or allocate heavily before validation clamps the request, violating proof, filter, and index queries must remain bounded and not expose stale security-relevant data, causing Low (501 - 2000 points). Any other important performance improvements for CKB?

## Target
- File/function: `rpc/src/module/experiment.rs::calculate_dao_maximum_withdraw`
- Entrypoint: an indexer/RPC client requesting adversarial ranges, filters, cursors, and pagination values
- Attacker controls: block/template parameters, transaction payloads, fee-rate values, and debug/experiment options
- Exploit idea: make RPC/indexer code panic or allocate heavily before validation clamps the request
- Invariant to test: proof, filter, and index queries must remain bounded and not expose stale security-relevant data
- Expected Immunefi impact: Low (501 - 2000 points). Any other important performance improvements for CKB
- Fast validation: Run a local RPC/indexer test with crafted JSON parameters and reorg timing; assert no panic and canonical, bounded responses.
