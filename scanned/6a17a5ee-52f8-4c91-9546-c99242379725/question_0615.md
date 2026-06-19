# Q615: High consensus canonical encoding ambiguity in from

## Question
Can an unprivileged attacker craft alternate encodings for header timestamp, compact target, epoch fraction, nonce, parent hash, and block number through a sync peer delivering reordered headers, uncles, and block extensions so `from` in `verification/src/cache.rs` accepts two representations for one security object and make fork-choice or delayed verification commit a block whose derived roots or epoch data disagree with recalculation, violating fork choice and verification caches must remain consistent across reorg, restart, and delayed verification, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `verification/src/cache.rs::from`
- Entrypoint: a sync peer delivering reordered headers, uncles, and block extensions
- Attacker controls: header timestamp, compact target, epoch fraction, nonce, parent hash, and block number
- Exploit idea: make fork-choice or delayed verification commit a block whose derived roots or epoch data disagree with recalculation
- Invariant to test: fork choice and verification caches must remain consistent across reorg, restart, and delayed verification
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
