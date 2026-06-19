# Q639: Critical consensus restart reorg persistence in is_too_new

## Question
Can an unprivileged attacker shape genesis/spec fields on a private chain and canonical block metadata during replay through an RPC block submitter feeding locally generated consensus objects, then force normal restart, reorg, retry, or replay handling so `is_too_new` in `verification/src/error.rs` persists inconsistent state and make contextual verification consume stale parent or epoch state after a reorg/orphan transition, violating fork choice and verification caches must remain consistent across reorg, restart, and delayed verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `verification/src/error.rs::is_too_new`
- Entrypoint: an RPC block submitter feeding locally generated consensus objects
- Attacker controls: genesis/spec fields on a private chain and canonical block metadata during replay
- Exploit idea: make contextual verification consume stale parent or epoch state after a reorg/orphan transition
- Invariant to test: fork choice and verification caches must remain consistent across reorg, restart, and delayed verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
