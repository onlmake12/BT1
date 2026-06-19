# Q3391: Critical txpool differential path split in CandidateUncles

## Question
Can an unprivileged attacker reach `CandidateUncles` in `tx-pool/src/block_assembler/candidate_uncles.rs` through two production paths from a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions and make one path accept while the other rejects because of verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `tx-pool/src/block_assembler/candidate_uncles.rs::CandidateUncles`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
