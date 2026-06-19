# Q3926: Critical vm state transition mismatch in store_data

## Question
Can an unprivileged attacker enter through a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles and sequence RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors so `store_data` in `script/src/syscalls/utils.rs` observes pre-state and post-state from different views, letting the flow make VM version gating select the wrong behavior at a hardfork boundary, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/utils.rs::store_data`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: RISC-V bytecode, argv/env, witness bytes, syscall offsets, lengths, indexes, and source selectors
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
