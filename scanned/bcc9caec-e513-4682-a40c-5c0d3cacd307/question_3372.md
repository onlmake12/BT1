# Q3372: Critical txpool differential path split in EaglesongSimple

## Question
Can an unprivileged attacker reach `EaglesongSimple` in `miner/src/worker/eaglesong_simple.rs` through two production paths from a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions and make one path accept while the other rejects because of block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `miner/src/worker/eaglesong_simple.rs::EaglesongSimple`
- Entrypoint: a transaction sender repeatedly submitting package, orphan, replacement, and low-fee transactions
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
