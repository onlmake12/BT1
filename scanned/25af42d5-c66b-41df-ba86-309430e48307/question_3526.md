# Q3526: Critical txpool canonical encoding ambiguity in remove_txs

## Question
Can an unprivileged attacker craft alternate encodings for verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions so `remove_txs` in `tx-pool/src/component/verify_queue.rs` accepts two representations for one security object and make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/component/verify_queue.rs::remove_txs`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
