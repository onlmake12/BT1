# Q3509: High txpool boundary divergence in fmt

## Question
Can an unprivileged attacker enter through a local miner process selecting proposals and uncles near limit boundaries and use transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status to drive `fmt` in `tx-pool/src/component/sort_key.rs` across a boundary where make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply, violating the invariant that pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions, causing High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs?

## Target
- File/function: `tx-pool/src/component/sort_key.rs::fmt`
- Entrypoint: a local miner process selecting proposals and uncles near limit boundaries
- Attacker controls: transaction fee, size, cycles, deps, ancestor/descendant graph, orphan parents, and proposal status
- Exploit idea: make tx-pool policy accept a transaction that block verification later rejects, or reject valid traffic cheaply
- Invariant to test: pool, orphan, recent-reject, and verify-queue resource use must stay bounded under attacker submissions
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs
- Fast validation: Simulate tx-pool submission, orphan/recent-reject transitions, and block-template assembly; assert bounded queues and consensus-valid output.
