# Q3080: High transaction boundary divergence in new

## Question
Can an unprivileged attacker enter through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values and use maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies to drive `new` in `util/light-client-protocol-server/src/components/get_transactions_proof.rs` across a boundary where bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization, violating the invariant that resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/light-client-protocol-server/src/components/get_transactions_proof.rs::new`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
