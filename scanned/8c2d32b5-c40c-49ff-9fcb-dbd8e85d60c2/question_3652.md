# Q3652: High txpool batch interaction bug in do_estimate

## Question
Can an unprivileged attacker batch duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions so `do_estimate` in `util/fee-estimator/src/estimator/weight_units_flow.rs` handles the first item safely but applies incorrect assumptions to later items and force quadratic graph or selection behavior with few low-cost transactions, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/fee-estimator/src/estimator/weight_units_flow.rs::do_estimate`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
