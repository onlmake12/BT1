# Q3870: High vm state transition mismatch in from

## Question
Can an unprivileged attacker enter through a transaction sender deploying a crafted CKB-VM script and witness payload and sequence cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data so `from` in `script/src/syscalls/mod.rs` observes pre-state and post-state from different views, letting the flow make a syscall expose bytes that differ from consensus-resolved transaction data, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts?

## Target
- File/function: `script/src/syscalls/mod.rs::from`
- Entrypoint: a transaction sender deploying a crafted CKB-VM script and witness payload
- Attacker controls: cycle limits, VM version, script hash type, loaded cell/header/tx/block-extension data
- Exploit idea: make a syscall expose bytes that differ from consensus-resolved transaction data
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
