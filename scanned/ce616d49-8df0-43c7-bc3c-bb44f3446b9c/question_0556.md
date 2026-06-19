# Q556: Critical consensus limit off by one in from

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for header timestamp, compact target, epoch fraction, nonce, parent hash, and block number through a sync peer delivering reordered headers, uncles, and block extensions so `from` in `spec/src/versionbits/convert.rs` trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path, violating malformed consensus objects must return structured errors without node panic or unbounded work, causing Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation?

## Target
- File/function: `spec/src/versionbits/convert.rs::from`
- Entrypoint: a sync peer delivering reordered headers, uncles, and block extensions
- Attacker controls: header timestamp, compact target, epoch fraction, nonce, parent hash, and block number
- Exploit idea: trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path
- Invariant to test: malformed consensus objects must return structured errors without node panic or unbounded work
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
