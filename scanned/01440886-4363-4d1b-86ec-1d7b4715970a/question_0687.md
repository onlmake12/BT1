# Q687: High consensus cross module inconsistency in disable_uncles

## Question
Can an unprivileged attacker use a remote peer relaying a crafted block/header sequence to make `disable_uncles` in `verification/traits/src/lib.rs` return a result that downstream modules interpret differently, where make contextual verification consume stale parent or epoch state after a reorg/orphan transition, violating all honest nodes must deterministically accept and reject the same blocks under the same consensus spec, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `verification/traits/src/lib.rs::disable_uncles`
- Entrypoint: a remote peer relaying a crafted block/header sequence
- Attacker controls: header timestamp, compact target, epoch fraction, nonce, parent hash, and block number
- Exploit idea: make contextual verification consume stale parent or epoch state after a reorg/orphan transition
- Invariant to test: all honest nodes must deterministically accept and reject the same blocks under the same consensus spec
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
