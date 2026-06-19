# Q3986: Critical vm restart reorg persistence in resumable_verify

## Question
Can an unprivileged attacker shape spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing through a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes, then force normal restart, reorg, retry, or replay handling so `resumable_verify` in `script/src/verify.rs` persists inconsistent state and make a syscall expose bytes that differ from consensus-resolved transaction data, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/verify.rs::resumable_verify`
- Entrypoint: a malicious script spawning or execing child processes with adversarial inherited descriptors and pipes
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: make a syscall expose bytes that differ from consensus-resolved transaction data
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
