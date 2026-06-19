# Q3700: High vm parser precheck gap in unknown_source

## Question
Can an unprivileged attacker submit malformed-but-reachable malformed buffers, return codes, memory pointers, and script group composition through a transaction sender deploying a crafted CKB-VM script and witness payload so `unknown_source` in `script/src/error.rs` performs expensive or unsafe work before validation and make VM version gating select the wrong behavior at a hardfork boundary, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/error.rs::unknown_source`
- Entrypoint: a transaction sender deploying a crafted CKB-VM script and witness payload
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
