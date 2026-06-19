# Q3943: High vm parser precheck gap in ecall

## Question
Can an unprivileged attacker submit malformed-but-reachable malformed buffers, return codes, memory pointers, and script group composition through a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles so `ecall` in `script/src/syscalls/wait.rs` performs expensive or unsafe work before validation and make a syscall expose bytes that differ from consensus-resolved transaction data, violating cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/syscalls/wait.rs::ecall`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: make a syscall expose bytes that differ from consensus-resolved transaction data
- Invariant to test: cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
