# Q531: Critical consensus batch interaction bug in HardForkConfig

## Question
Can an unprivileged attacker batch genesis/spec fields on a private chain and canonical block metadata during replay through a remote peer relaying a crafted block/header sequence so `HardForkConfig` in `spec/src/hardfork.rs` handles the first item safely but applies incorrect assumptions to later items and make contextual verification consume stale parent or epoch state after a reorg/orphan transition, violating fork choice and verification caches must remain consistent across reorg, restart, and delayed verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `spec/src/hardfork.rs::HardForkConfig`
- Entrypoint: a remote peer relaying a crafted block/header sequence
- Attacker controls: genesis/spec fields on a private chain and canonical block metadata during replay
- Exploit idea: make contextual verification consume stale parent or epoch state after a reorg/orphan transition
- Invariant to test: fork choice and verification caches must remain consistent across reorg, restart, and delayed verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
