# Q3548: Critical txpool batch interaction bug in lib

## Question
Can an unprivileged attacker batch block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a miner/RPC block-template caller assembling blocks from adversarial tx-pool state so `lib` in `tx-pool/src/lib.rs` handles the first item safely but applies incorrect assumptions to later items and make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `tx-pool/src/lib.rs::lib`
- Entrypoint: a miner/RPC block-template caller assembling blocks from adversarial tx-pool state
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
