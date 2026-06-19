# Q3881: High vm boundary divergence in Syscalls

## Question
Can an unprivileged attacker enter through a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles and use RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors to drive `Syscalls` in `script/src/syscalls/pipe.rs` across a boundary where make a syscall expose bytes that differ from consensus-resolved transaction data, violating the invariant that malformed syscall arguments must fail safely without node crash or authorization bypass, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/syscalls/pipe.rs::Syscalls`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors
- Exploit idea: make a syscall expose bytes that differ from consensus-resolved transaction data
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
