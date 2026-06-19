# Q422: Critical consensus canonical encoding ambiguity in detached_blocks

## Question
Can an unprivileged attacker craft alternate encodings for header timestamp, compact target, epoch fraction, nonce, parent hash, and block number through a remote peer relaying a crafted block/header sequence so `detached_blocks` in `chain/src/utils/forkchanges.rs` accepts two representations for one security object and trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path, violating invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `chain/src/utils/forkchanges.rs::detached_blocks`
- Entrypoint: a remote peer relaying a crafted block/header sequence
- Attacker controls: header timestamp, compact target, epoch fraction, nonce, parent hash, and block number
- Exploit idea: trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path
- Invariant to test: invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
