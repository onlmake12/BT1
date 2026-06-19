# Q498: Critical consensus restart reorg persistence in is_dummy

## Question
Can an unprivileged attacker shape genesis/spec fields on a private chain and canonical block metadata during replay through a remote peer relaying a crafted block/header sequence, then force normal restart, reorg, retry, or replay handling so `is_dummy` in `pow/src/lib.rs` persists inconsistent state and make fork-choice or delayed verification commit a block whose derived roots or epoch data disagree with recalculation, violating all honest nodes must deterministically accept and reject the same blocks under the same consensus spec, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `pow/src/lib.rs::is_dummy`
- Entrypoint: a remote peer relaying a crafted block/header sequence
- Attacker controls: genesis/spec fields on a private chain and canonical block metadata during replay
- Exploit idea: make fork-choice or delayed verification commit a block whose derived roots or epoch data disagree with recalculation
- Invariant to test: all honest nodes must deterministically accept and reject the same blocks under the same consensus spec
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
