# Q3574: High txpool limit off by one in orphan_contains

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions so `orphan_contains` in `tx-pool/src/process.rs` make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/process.rs::orphan_contains`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
