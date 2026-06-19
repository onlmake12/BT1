# Q3832: Critical vm limit off by one in LoadScriptHash

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for malformed buffers, return codes, memory pointers, and script group composition through a transaction sender deploying a crafted CKB-VM script and witness payload so `LoadScriptHash` in `script/src/syscalls/load_script_hash.rs` trigger a VM panic or host-side bounds error before the transaction is rejected, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/load_script_hash.rs::LoadScriptHash`
- Entrypoint: a transaction sender deploying a crafted CKB-VM script and witness payload
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: trigger a VM panic or host-side bounds error before the transaction is rejected
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
