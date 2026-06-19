# Q3755: Critical vm canonical encoding ambiguity in fetch_witness

## Question
Can an unprivileged attacker craft alternate encodings for spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing through a transaction sender deploying a crafted CKB-VM script and witness payload so `fetch_witness` in `script/src/syscalls/exec.rs` accepts two representations for one security object and make VM version gating select the wrong behavior at a hardfork boundary, violating malformed syscall arguments must fail safely without node crash or authorization bypass, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/exec.rs::fetch_witness`
- Entrypoint: a transaction sender deploying a crafted CKB-VM script and witness payload
- Attacker controls: spawn/exec metadata, pipe/read/write order, inherited fds, process IDs, and wait/close timing
- Exploit idea: make VM version gating select the wrong behavior at a hardfork boundary
- Invariant to test: malformed syscall arguments must fail safely without node crash or authorization bypass
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
