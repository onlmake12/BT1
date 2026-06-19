# Q3670: Critical txpool cache invalidation failure in lib

## Question
Can an unprivileged attacker use a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions to alternate valid and invalid block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples so `lib` in `util/fee-estimator/src/lib.rs` leaves a cache, index, or status flag stale and make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fee-estimator/src/lib.rs::lib`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
