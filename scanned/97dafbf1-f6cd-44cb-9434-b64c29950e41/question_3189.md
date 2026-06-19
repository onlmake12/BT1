# Q3189: Critical transaction batch interaction bug in try_from

## Question
Can an unprivileged attacker batch input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries through a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values so `try_from` in `util/types/src/core/blockchain.rs` handles the first item safely but applies incorrect assumptions to later items and make non-contextual, contextual, and tx-pool verification disagree about the same transaction, violating resolved transaction data must bind exactly to the canonical cells and headers used by scripts, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `util/types/src/core/blockchain.rs::try_from`
- Entrypoint: a transaction sender submitting crafted inputs, outputs, deps, witnesses, and since values
- Attacker controls: input/output ordering, type-id positions, transaction size, cycles, and fee-rate boundaries
- Exploit idea: make non-contextual, contextual, and tx-pool verification disagree about the same transaction
- Invariant to test: resolved transaction data must bind exactly to the canonical cells and headers used by scripts
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Create a local transaction/block verification harness with crafted resolved cells and compare tx-pool, non-contextual, and contextual results.
