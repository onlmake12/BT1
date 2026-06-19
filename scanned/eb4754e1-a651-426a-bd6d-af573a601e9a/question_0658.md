# Q658: High consensus limit off by one in new

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for genesis/spec fields on a private chain and canonical block metadata during replay through a miner on a private chain producing valid-PoW boundary blocks so `new` in `verification/src/header_verifier.rs` force two verification paths to classify the same block differently around a boundary check, violating malformed consensus objects must return structured errors without node panic or unbounded work, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `verification/src/header_verifier.rs::new`
- Entrypoint: a miner on a private chain producing valid-PoW boundary blocks
- Attacker controls: genesis/spec fields on a private chain and canonical block metadata during replay
- Exploit idea: force two verification paths to classify the same block differently around a boundary check
- Invariant to test: malformed consensus objects must return structured errors without node panic or unbounded work
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
