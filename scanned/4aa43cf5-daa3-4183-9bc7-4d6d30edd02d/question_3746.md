# Q3746: Critical vm differential path split in ecall

## Question
Can an unprivileged attacker reach `ecall` in `script/src/syscalls/debugger.rs` through two production paths from a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles and make one path accept while the other rejects because of RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/debugger.rs::ecall`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
