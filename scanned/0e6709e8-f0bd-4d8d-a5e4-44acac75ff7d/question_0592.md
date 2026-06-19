# Q592: Critical consensus parser precheck gap in UnclesVerifier

## Question
Can an unprivileged attacker submit malformed-but-reachable header timestamp, compact target, epoch fraction, nonce, parent hash, and block number through an RPC block submitter feeding locally generated consensus objects so `UnclesVerifier` in `verification/contextual/src/uncles_verifier.rs` performs expensive or unsafe work before validation and make contextual verification consume stale parent or epoch state after a reorg/orphan transition, violating all honest nodes must deterministically accept and reject the same blocks under the same consensus spec, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `verification/contextual/src/uncles_verifier.rs::UnclesVerifier`
- Entrypoint: an RPC block submitter feeding locally generated consensus objects
- Attacker controls: header timestamp, compact target, epoch fraction, nonce, parent hash, and block number
- Exploit idea: make contextual verification consume stale parent or epoch state after a reorg/orphan transition
- Invariant to test: all honest nodes must deterministically accept and reject the same blocks under the same consensus spec
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
