# Q459: Critical consensus resource amplification in update_proposal_table

## Question
Can an unprivileged attacker repeatedly send small fork order, orphan arrival timing, hardfork activation boundary, and reorg depth through an RPC block submitter feeding locally generated consensus objects to make `update_proposal_table` in `chain/src/verify.rs` amplify CPU, memory, storage, or bandwidth and trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path, violating invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `chain/src/verify.rs::update_proposal_table`
- Entrypoint: an RPC block submitter feeding locally generated consensus objects
- Attacker controls: fork order, orphan arrival timing, hardfork activation boundary, and reorg depth
- Exploit idea: trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path
- Invariant to test: invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
