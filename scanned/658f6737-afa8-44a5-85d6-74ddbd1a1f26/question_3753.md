# Q3753: High vm batch interaction bug in ecall

## Question
Can an unprivileged attacker batch cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes so `ecall` in `script/src/syscalls/exec.rs` handles the first item safely but applies incorrect assumptions to later items and make a syscall expose bytes that differ from consensus-resolved transaction data, violating cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/syscalls/exec.rs::ecall`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data
- Exploit idea: make a syscall expose bytes that differ from consensus-resolved transaction data
- Invariant to test: cycle accounting and process state must remain bounded and cannot be bypassed by crafted scripts
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
