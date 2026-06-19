# Q379: High consensus limit off by one in start_chain_services

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for fork order, orphan arrival timing, hardfork activation boundary, and reorg depth through a remote peer relaying a crafted block/header sequence so `start_chain_services` in `chain/src/init.rs` trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path, violating invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `chain/src/init.rs::start_chain_services`
- Entrypoint: a remote peer relaying a crafted block/header sequence
- Attacker controls: fork order, orphan arrival timing, hardfork activation boundary, and reorg depth
- Exploit idea: trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path
- Invariant to test: invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
