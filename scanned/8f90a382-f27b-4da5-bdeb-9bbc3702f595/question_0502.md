# Q502: Critical consensus restart reorg persistence in consensus_spec

## Question
Can an unprivileged attacker shape genesis/spec fields on a private chain and canonical block metadata during replay through a miner on a private chain producing valid-PoW boundary blocks, then force normal restart, reorg, retry, or replay handling so `consensus_spec` in `resource/specs/mainnet.toml` persists inconsistent state and make contextual verification consume stale parent or epoch state after a reorg/orphan transition, violating invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical, causing Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network?

## Target
- File/function: `resource/specs/mainnet.toml::consensus_spec`
- Entrypoint: a miner on a private chain producing valid-PoW boundary blocks
- Attacker controls: genesis/spec fields on a private chain and canonical block metadata during replay
- Exploit idea: make contextual verification consume stale parent or epoch state after a reorg/orphan transition
- Invariant to test: invalid PoW, epoch, uncle, proposal, extension, DAO, or root data must never become canonical
- Expected Immunefi impact: Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network
- Fast validation: Build a focused unit or integration test with the target verifier and a private-chain consensus spec; assert identical accept/reject results before and after reorg/restart.
