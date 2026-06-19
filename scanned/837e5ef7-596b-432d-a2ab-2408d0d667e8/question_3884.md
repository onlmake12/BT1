# Q3884: Critical vm replay reorder race in Syscalls

## Question
Can an unprivileged attacker replay, reorder, or delay RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes so `Syscalls` in `script/src/syscalls/pipe.rs` takes a stale branch and make a syscall expose bytes that differ from consensus-resolved transaction data, breaking the invariant that malformed syscall arguments must fail safely without node crash or authorization bypass, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/pipe.rs::Syscalls`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors
- Exploit idea: make a syscall expose bytes that differ from consensus-resolved transaction data
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
