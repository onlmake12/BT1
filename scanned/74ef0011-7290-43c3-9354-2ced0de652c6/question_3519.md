# Q3519: Critical txpool limit off by one in txs_to_commit

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a local miner process selecting proposals and uncles near limit boundaries so `txs_to_commit` in `tx-pool/src/component/tx_selector.rs` force quadratic graph or selection behavior with few low-cost transactions, violating pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `tx-pool/src/component/tx_selector.rs::txs_to_commit`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: force quadratic graph or selection behavior with few low-cost transactions
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
