# Q3631: High txpool restart reorg persistence in add_confirmed_tx

## Question
Can an unprivileged attacker shape block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a peer relaying transactions that race recent-reject, orphan, and verification-queue state, then force normal restart, reorg, retry, or replay handling so `add_confirmed_tx` in `util/fee-estimator/src/estimator/confirmation_fraction.rs` persists inconsistent state and make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating valid user transactions must not be persistently censored by cheap attacker-created pool state, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/fee-estimator/src/estimator/confirmation_fraction.rs::add_confirmed_tx`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: valid user transactions must not be persistently censored by cheap attacker-created pool state
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
