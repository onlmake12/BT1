# Q3476: High txpool resource amplification in new

## Question
Can an unprivileged attacker repeatedly send small transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status through a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions to make `new` in `tx-pool/src/component/orphan.rs` amplify CPU, memory, storage, or bandwidth and force quadratic graph or selection behavior with few low-cost transactions, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/component/orphan.rs::new`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
