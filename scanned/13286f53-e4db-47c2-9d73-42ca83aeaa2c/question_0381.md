# Q381: High consensus resource amplification in InitLoadUnverified

## Question
Can an unprivileged attacker repeatedly send small genesis/spec fields on a private chain and canonical block metadata during replay through an RPC block submitter feeding locally generated consensus objects to make `InitLoadUnverified` in `chain/src/init_load_unverified.rs` amplify CPU, memory, storage, or bandwidth and force two verification paths to classify the same block differently around a boundary check, violating invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `chain/src/init_load_unverified.rs::InitLoadUnverified`
- Entrypoint: an RPC block submitter feeding locally generated consensus objects
- Attacker controls: genesis/spec fields on a private chain and canonical block metadata during replay
- Exploit idea: force two verification paths to classify the same block differently around a boundary check
- Invariant to test: invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
