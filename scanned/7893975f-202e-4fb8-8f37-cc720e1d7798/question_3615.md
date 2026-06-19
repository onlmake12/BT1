# Q3615: High txpool parser precheck gap in constants

## Question
Can an unprivileged attacker submit malformed-but-reachable block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples through a local miner process selecting proposals and uncles near limit boundaries so `constants` in `util/fee-estimator/src/constants.rs` performs expensive or unsafe work before validation and force quadratic graph or selection behavior with few low-cost transactions, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `util/fee-estimator/src/constants.rs::constants`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: block template limits, tx selection order, uncle candidates, proposal IDs, and fee estimator samples
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
