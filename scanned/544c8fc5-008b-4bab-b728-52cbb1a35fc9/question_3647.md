# Q3647: Critical txpool parser precheck gap in new_confirmation_fraction

## Question
Can an unprivileged attacker submit malformed-but-reachable transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions so `new_confirmation_fraction` in `util/fee-estimator/src/estimator/mod.rs` performs expensive or unsafe work before validation and make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fee-estimator/src/estimator/mod.rs::new_confirmation_fraction`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
