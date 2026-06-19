# Q3760: High vm batch interaction bug in resolved_inputs

## Question
Can an unprivileged attacker batch cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data through a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles so `resolved_inputs` in `script/src/syscalls/exec.rs` handles the first item safely but applies incorrect assumptions to later items and undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `script/src/syscalls/exec.rs::resolved_inputs`
- Entrypoint: a script author invoking syscalls with boundary offsets, lengths, file descriptors, and process handles
- Attacker controls: cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data
- Exploit idea: undercharge cycles or bypass a failure path by racing spawn/exec/read/write state transitions
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
