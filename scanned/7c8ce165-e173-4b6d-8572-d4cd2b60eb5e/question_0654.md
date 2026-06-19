# Q654: Critical consensus replay reorder race in PowVerifier

## Question
Can an unprivileged attacker replay, reorder, or delay fork order, orphan arrival timing, hardfork activation boundary, and reorg depth through a remote peer relaying a crafted block/header sequence so `PowVerifier` in `verification/src/header_verifier.rs` takes a stale branch and trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path, breaking the invariant that fork choice and verification caches must remain consistent across reorg, restart, and delayed verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `verification/src/header_verifier.rs::PowVerifier`
- Entrypoint: a remote peer relaying a crafted block/header sequence
- Attacker controls: fork order, orphan arrival timing, hardfork activation boundary, and reorg depth
- Exploit idea: trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path
- Invariant to test: fork choice and verification caches must remain consistent across reorg, restart, and delayed verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
