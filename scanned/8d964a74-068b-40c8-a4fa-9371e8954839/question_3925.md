# Q3925: Critical vm differential path split in store_data

## Question
Can an unprivileged attacker reach `store_data` in `script/src/syscalls/utils.rs` through two production paths from a transaction sender deploying a crafted CKB-VM script and witness payload and make one path accept while the other rejects because of malformed buffers, return codes, memory pointers, and script group composition, violating scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `script/src/syscalls/utils.rs::store_data`
- Entrypoint: a transaction sender deploying a crafted CKB-VM script and witness payload
- Attacker controls: malformed buffers, return codes, memory pointers, and script group composition
- Exploit idea: make a syscall expose bytes that differ from consensus-resolved transaction data
- Invariant to test: scripts must see exactly the resolved cells, headers, witnesses, and block extensions committed by consensus
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Add a CKB-VM syscall/script regression test with boundary arguments and assert return code, cycles, and script-visible bytes.
