# Q3648: Critical txpool state transition mismatch in reject_tx

## Question
Can an unprivileged attacker enter through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state and sequence duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions so `reject_tx` in `util/fee-estimator/src/estimator/mod.rs` observes pre-state and post-state from different views, letting the flow make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/fee-estimator/src/estimator/mod.rs::reject_tx`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: duplicate hashes, conflicted inputs, dep-heavy packages, and repeated rejected submissions
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
