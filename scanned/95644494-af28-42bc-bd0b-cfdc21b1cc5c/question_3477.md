# Q3477: Critical txpool restart reorg persistence in new

## Question
Can an unprivileged attacker shape block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a local miner process selecting proposals and uncles near limit boundaries, then force normal restart, reorg, retry, or replay handling so `new` in `tx-pool/src/component/orphan.rs` persists inconsistent state and make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/component/orphan.rs::new`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
