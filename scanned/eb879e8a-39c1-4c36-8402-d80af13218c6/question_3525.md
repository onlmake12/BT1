# Q3525: Critical txpool canonical encoding ambiguity in remove_txs

## Question
Can an unprivileged attacker craft alternate encodings for transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions so `remove_txs` in `tx-pool/src/component/verify_queue.rs` accepts two representations for one security object and force quadratic graph or selection behavior with few low-cost transactions, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/component/verify_queue.rs::remove_txs`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
