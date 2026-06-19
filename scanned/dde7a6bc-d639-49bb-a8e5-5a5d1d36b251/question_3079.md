# Q3079: Critical transaction cache invalidation failure in new

## Question
Can an unprivileged attacker use a block relayer including dependency-heavy transactions in an otherwise valid block to alternate valid and invalid maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies so `new` in `util/light-client-protocol-server/src/components/get_transactions_proof.rs` leaves a cache, index, or status flag stale and make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy?

## Target
- File/function: `util/light-client-protocol-server/src/components/get_transactions_proof.rs::new`
- Entrypoint: a block relayer including dependency-heavy transactions in an otherwise valid block
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
