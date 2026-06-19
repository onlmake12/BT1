# Q659: Critical consensus limit off by one in new

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for header timestamp, compact target, epoch fraction, nonce, parent hash, and block number through a remote peer relaying a crafted block/header sequence so `new` in `verification/src/header_verifier.rs` force two verification paths to classify the same block differently around a boundary check, violating fork choice and verification caches must remain consistent across reorg, restart, and delayed verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `verification/src/header_verifier.rs::new`
- Entrypoint: a remote peer relaying a crafted block/header sequence
- Attacker controls: header timestamp, compact target, epoch fraction, nonce, parent hash, and block number
- Exploit idea: force two verification paths to classify the same block differently around a boundary check
- Invariant to test: fork choice and verification caches must remain consistent across reorg, restart, and delayed verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
