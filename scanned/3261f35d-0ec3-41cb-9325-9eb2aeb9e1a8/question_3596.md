# Q3596: Critical txpool limit off by one in non_contextual_verify

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions so `non_contextual_verify` in `tx-pool/src/util.rs` make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/util.rs::non_contextual_verify`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
