# Q616: Critical consensus boundary divergence in from

## Question
Can an unprivileged attacker enter through a sync peer delivering reordered headers, uncles, and block extensions and use header timestamp, compact target, epoch fraction, nonce, parent hash, and block number to drive `from` in `verification/src/cache.rs` across a boundary where make contextual verification consume stale parent or epoch state after a reorg/orphan transition, violating the invariant that fork choice and verification caches must remain consistent across reorg, restart, and delayed verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `verification/src/cache.rs::from`
- Entrypoint: a sync peer delivering reordered headers, uncles, and block extensions
- Attacker controls: header timestamp, compact target, epoch fraction, nonce, parent hash, and block number
- Exploit idea: make contextual verification consume stale parent or epoch state after a reorg/orphan transition
- Invariant to test: fork choice and verification caches must remain consistent across reorg, restart, and delayed verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
