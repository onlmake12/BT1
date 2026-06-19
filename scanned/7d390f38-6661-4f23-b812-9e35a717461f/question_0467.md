# Q467: High consensus restart reorg persistence in verify

## Question
Can an unprivileged attacker shape uncle lists, proposal IDs, block extension bytes, transaction roots, and DAO fields through a remote peer relaying a crafted block/header sequence, then force normal restart, reorg, retry, or replay handling so `verify` in `pow/src/dummy.rs` persists inconsistent state and make contextual verification consume stale parent or epoch state after a reorg/orphan transition, violating invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `pow/src/dummy.rs::verify`
- Entrypoint: a remote peer relaying a crafted block/header sequence
- Attacker controls: uncle lists, proposal IDs, block extension bytes, transaction roots, and DAO fields
- Exploit idea: make contextual verification consume stale parent or epoch state after a reorg/orphan transition
- Invariant to test: invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
