# Q619: High consensus restart reorg persistence in from

## Question
Can an unprivileged attacker shape genesis/spec fields on a private chain and canonical block metadata during replay through an RPC block submitter feeding locally generated consensus objects, then force normal restart, reorg, retry, or replay handling so `from` in `verification/src/cache.rs` persists inconsistent state and force two verification paths to classify the same block differently around a boundary check, violating invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `verification/src/cache.rs::from`
- Entrypoint: an RPC block submitter feeding locally generated consensus objects
- Attacker controls: genesis/spec fields on a private chain and canonical block metadata during replay
- Exploit idea: force two verification paths to classify the same block differently around a boundary check
- Invariant to test: invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
