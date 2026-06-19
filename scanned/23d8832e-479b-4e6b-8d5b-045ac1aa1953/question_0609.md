# Q609: High consensus limit off by one in NonContextualBlockTxsVerifier

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for header timestamp, compact target, epoch fraction, nonce, parent hash, and block number through a remote peer relaying a crafted block/header sequence so `NonContextualBlockTxsVerifier` in `verification/src/block_verifier.rs` trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path, violating malformed consensus objects must return structured errors without node panic or unbounded work, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `verification/src/block_verifier.rs::NonContextualBlockTxsVerifier`
- Entrypoint: a remote peer relaying a crafted block/header sequence
- Attacker controls: header timestamp, compact target, epoch fraction, nonce, parent hash, and block number
- Exploit idea: trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path
- Invariant to test: malformed consensus objects must return structured errors without node panic or unbounded work
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
