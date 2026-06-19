# Q3885: High vm boundary divergence in ecall

## Question
Can an unprivileged attacker enter through a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles and use malformed buffers, return codes, memory pointers, and script group composition to drive `ecall` in `script/src/syscalls/pipe.rs` across a boundary where trigger a VM panic or host-side bounds error before the transaction is rejected, violating the invariant that CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/syscalls/pipe.rs::ecall`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: CKB-VM and syscall behavior must be deterministic and consensus-equivalent across nodes
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
