# Q3827: Critical vm differential path split in ecall

## Question
Can an unprivileged attacker reach `ecall` in `script/src/syscalls/load_script.rs` through two production paths from a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles and make one path accept while the other rejects because of RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors, violating cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/load_script.rs::ecall`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
