# Q3796: Critical vm canonical encoding ambiguity in header_deps

## Question
Can an unprivileged attacker craft alternate encodings for malformed buffers, return codes, memory pointers, and script group composition through a transaction sender deploying a crafted CKB-VM script and witness payload so `header_deps` in `script/src/syscalls/load_block_extension.rs` accepts two representations for one security object and make a syscall expose bytes that differ from consensus-resolved transaction data, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/load_block_extension.rs::header_deps`
- Entrypoint: a transaction sender deploying a crafted CKB-VM script and witness payload
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: make a syscall expose bytes that differ from consensus-resolved transaction data
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
