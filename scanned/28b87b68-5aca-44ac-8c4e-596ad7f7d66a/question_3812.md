# Q3812: High vm cache invalidation failure in ecall

## Question
Can an unprivileged attacker use a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles to alternate valid and invalid malformed buffers, return codes, memory pointers, and script group composition so `ecall` in `script/src/syscalls/load_input.rs` leaves a cache, index, or status flag stale and make VM version gating select the wrong behavior at a hardfork boundary, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/syscalls/load_input.rs::ecall`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
