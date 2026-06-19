# Q523: High consensus limit off by one in From

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for genesis/spec fields on a private chain and canonical block metadata during replay through a remote peer relaying a crafted block/header sequence so `From` in `spec/src/error.rs` force two verification paths to classify the same block differently around a boundary check, violating fork choice and verification caches must remain consistent across reorg, restart, and delayed verification, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `spec/src/error.rs::From`
- Entrypoint: a remote peer relaying a crafted block/header sequence
- Attacker controls: genesis/spec fields on a private chain and canonical block metadata during replay
- Exploit idea: force two verification paths to classify the same block differently around a boundary check
- Invariant to test: fork choice and verification caches must remain consistent across reorg, restart, and delayed verification
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
