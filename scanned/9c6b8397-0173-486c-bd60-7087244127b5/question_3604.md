# Q3604: High txpool resource amplification in new

## Question
Can an unprivileged attacker repeatedly send small verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a local miner process selecting proposals and uncles near limit boundaries to make `new` in `tx-pool/src/verify_mgr.rs` amplify CPU, memory, storage, or bandwidth and make block assembly include invalid, duplicate, or economically wrong transactions or rewards, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/verify_mgr.rs::new`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: make block assembly include invalid, duplicate, or economically wrong transactions or rewards
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
