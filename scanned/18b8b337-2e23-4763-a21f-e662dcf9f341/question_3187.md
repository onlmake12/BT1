# Q3187: High transaction batch interaction bug in try_from

## Question
Can an unprivileged attacker batch maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values so `try_from` in `util/types/src/core/blockchain.rs` handles the first item safely but applies incorrect assumptions to later items and bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization, violating capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `util/types/src/core/blockchain.rs::try_from`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: maturity height, since flags, DAO fields, resolved transaction data, and duplicate dependencies
- Exploit idea: bypass a conservation, maturity, since, or occupied-capacity check through boundary serialization
- Invariant to test: capacity must be conserved and occupied-capacity, maturity, and since rules must be deterministic
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
