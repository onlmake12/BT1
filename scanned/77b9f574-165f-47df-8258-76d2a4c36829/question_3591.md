# Q3591: Critical txpool canonical encoding ambiguity in check_tx_fee

## Question
Can an unprivileged attacker craft alternate encodings for block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a peer relaying transactions that race recent-reject, orphan, and verification-queue state so `check_tx_fee` in `tx-pool/src/util.rs` accepts two representations for one security object and force quadratic graph or selection behavior with few low-cost transactions, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/util.rs::check_tx_fee`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
