# Q388: High consensus boundary divergence in new

## Question
Can an unprivileged attacker enter through a miner on a private chain producing valid-PoW boundary blocks and use header timestamp, compact target, epoch fraction, nonce, parent hash, and block number to drive `new` in `chain/src/init_load_unverified.rs` across a boundary where make contextual verification consume stale parent or epoch state after a reorg/orphan transition, violating the invariant that fork choice and verification caches must remain consistent across reorg, restart, and delayed verification, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `chain/src/init_load_unverified.rs::new`
- Entrypoint: a miner on a private chain producing valid-PoW boundary blocks
- Attacker controls: header timestamp, compact target, epoch fraction, nonce, parent hash, and block number
- Exploit idea: make contextual verification consume stale parent or epoch state after a reorg/orphan transition
- Invariant to test: fork choice and verification caches must remain consistent across reorg, restart, and delayed verification
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
