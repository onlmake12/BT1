# Q3619: High txpool restart reorg persistence in constants

## Question
Can an unprivileged attacker shape transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status through a peer relaying transactions that race recent-reject, orphan, and verification-queue state, then force normal restart, reorg, retry, or replay handling so `constants` in `util/fee-estimator/src/constants.rs` persists inconsistent state and make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating tx-pool admission must remain a safe prefilter for consensus block verification, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/fee-estimator/src/constants.rs::constants`
- Entrypoint: a peer relaying transactions that race recent-reject, orphan, and verification-queue state
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: tx-pool admission must remain a safe prefilter for consensus block verification
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
