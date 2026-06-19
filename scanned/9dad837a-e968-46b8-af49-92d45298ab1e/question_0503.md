# Q503: High consensus limit off by one in consensus_spec

## Question
Can an unprivileged attacker choose exact minimum, maximum, empty, duplicate, or plus-one values for header timestamp, compact target, epoch fraction, nonce, parent hash, and block number through a miner on a private chain producing valid-PoW boundary blocks so `consensus_spec` in `resource/specs/mainnet.toml` trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path, violating all honest nodes must deterministically accept and reject the same blocks under the same consensus spec, causing High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node?

## Target
- File/function: `resource/specs/mainnet.toml::consensus_spec`
- Entrypoint: a miner on a private chain producing valid-PoW boundary blocks
- Attacker controls: header timestamp, compact target, epoch fraction, nonce, parent hash, and block number
- Exploit idea: trigger an arithmetic, encoding, or target-conversion edge before the normal consensus rejection path
- Invariant to test: all honest nodes must deterministically accept and reject the same blocks under the same consensus spec
- Expected Immunefi impact: High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
