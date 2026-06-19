# Q3387: Critical txpool limit off by one in partition_nonce

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing through a local miner process selecting proposals and uncles near limit boundaries so `partition_nonce` in `miner/src/worker/mod.rs` make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `miner/src/worker/mod.rs::partition_nonce`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: verify-queue ordering, recent-reject keys, pool capacity pressure, and reorg timing
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: block assembly must preserve consensus validity, proposal eligibility, reward, and fee accounting
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
