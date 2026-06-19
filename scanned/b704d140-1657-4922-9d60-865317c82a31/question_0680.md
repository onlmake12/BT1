# Q680: Critical consensus state transition mismatch in disable_all

## Question
Can an unprivileged attacker enter through a miner on a private chain producing valid-PoW boundary blocks and sequence header timestamp, compact target, epoch fraction, nonce, parent hash, and block number so `disable_all` in `verification/traits/src/lib.rs` observes pre-state and post-state from different views, letting the flow trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path, violating invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `verification/traits/src/lib.rs::disable_all`
- Entrypoint: a miner on a private chain producing valid-PoW boundary blocks
- Attacker controls: header timestamp, compact target, epoch fraction, nonce, parent hash, and block number
- Exploit idea: trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path
- Invariant to test: invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
