# Q427: Critical consensus batch interaction bug in is_sorted

## Question
Can an unprivileged attacker batch genesis/spec fields on a private chain and canonical block metadata during replay through a miner on a private chain producing valid-PoW boundary blocks so `is_sorted` in `chain/src/utils/forkchanges.rs` handles the first item safely but applies incorrect assumptions to later items and force two verification paths to classify the same block differently around a boundary check, violating malformed consensus objects must return structured errors without node panic or unbounded work, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `chain/src/utils/forkchanges.rs::is_sorted`
- Entrypoint: a miner on a private chain producing valid-PoW boundary blocks
- Attacker controls: genesis/spec fields on a private chain and canonical block metadata during replay
- Exploit idea: force two verification paths to classify the same block differently around a boundary check
- Invariant to test: malformed consensus objects must return structured errors without node panic or unbounded work
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
