### Title
Integer Overflow in `GET_LAST_STATE_PROOF_LIMIT` Guard Enables Unbounded Server Work per Request — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary

The guard at line 201 of `GetLastStateProofProcess::execute` uses the expression `(last_n_blocks as usize) * 2` to enforce a cap of 1000 samples. When an attacker sends `last_n_blocks = 2^63` (`0x8000000000000000`), this multiplication wraps to `0` in release-mode Rust (no overflow panic), bypassing the guard entirely. The server then allocates a `Vec` of every block number from `start_block_number` to `last_block_number` and calls `chain_root_mmr(number-1).get_root()` for each — an expensive MMR operation — with no bound on the number of iterations.

### Finding Description

**Overflow in the guard:** [1](#0-0) 

`last_n_blocks` is decoded as a `u64` from the peer message. The cast `last_n_blocks as usize` is lossless on 64-bit targets (usize = 64 bits). The subsequent `* 2` on a value of `2^63` produces `2^64`, which wraps to `0` in release mode (Rust only panics on overflow in debug builds). With `difficulties.len() == 0`, the check becomes `0 + 0 > 1000` → `false`, and the guard is silently bypassed.

**Unbounded allocation after bypass:** [2](#0-1) 

Because `last_n_blocks = 2^63` is astronomically larger than any real chain length, the condition `last_block_number - start_block_number <= last_n_blocks` is always true. The server takes the "not enough blocks" branch and executes `(start_block_number..last_block_number).collect::<Vec<_>>()`, allocating a `Vec` containing every block number on the chain — potentially millions of entries.

**Expensive per-entry work:** [3](#0-2) 

`complete_headers` iterates every collected block number and calls `self.snapshot.chain_root_mmr(*number - 1).get_root()` for each, which is a non-trivial MMR computation. This means CPU and memory scale linearly with chain length, not with the intended limit of 1000.

**The constant being bypassed:** [4](#0-3) 

### Impact Explanation

Any unprivileged peer can send a single `GetLastStateProof` message with `last_n_blocks = 0x8000000000000000`, `difficulties = []`, and a valid tip hash (obtainable by normal chain sync). The server will allocate a `Vec` proportional to the full chain length and perform one MMR root computation per block. Repeated requests from multiple peers exhaust server memory and CPU, causing the full node to crash or become unresponsive. All light-client-serving nodes on the network are equally affected.

### Likelihood Explanation

The preconditions are trivially satisfiable: the attacker only needs a valid tip hash (available from normal sync) and the ability to craft a `GetLastStateProof` message with a specific `u64` field value. No PoW, no keys, no privileged access required. The overflow is deterministic and reproducible on any 64-bit host running a release build.

### Recommendation

Replace the unchecked arithmetic with overflow-safe operations:

```rust
// Instead of:
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT

// Use:
let last_n_blocks_doubled = (last_n_blocks as usize)
    .checked_mul(2)
    .unwrap_or(usize::MAX);
if self.message.difficulties().len().saturating_add(last_n_blocks_doubled)
    > constant::GET_LAST_STATE_PROOF_LIMIT
```

Additionally, add an explicit upper-bound check on `last_n_blocks` before any arithmetic:

```rust
if last_n_blocks > constant::GET_LAST_STATE_PROOF_LIMIT as u64 {
    return StatusCode::MalformedProtocolMessage.with_context("last_n_blocks too large");
}
```

### Proof of Concept

1. Sync a node to height N (e.g., N = 500,000).
2. Craft a `GetLastStateProof` message: `last_n_blocks = 0x8000000000000000`, `difficulties = []`, `start_number = 0`, `last_hash = tip_hash`, `difficulty_boundary = U256::MAX`.
3. Send to the server peer.
4. Observe: the guard check evaluates `0 + 0 > 1000 = false`; the server allocates a `Vec` of 500,000 `BlockNumber` entries and calls `chain_root_mmr` 500,000 times.
5. Assert `block_numbers.len() == N` (not bounded by 1000) and that server RSS grows proportionally to N.
6. Repeat from multiple peers to exhaust memory and crash the node.

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L150-163)
```rust
                let parent_chain_root = if *number == 0 {
                    Default::default()
                } else {
                    let mmr = self.snapshot.chain_root_mmr(*number - 1);
                    match mmr.get_root() {
                        Ok(root) => root,
                        Err(err) => {
                            let errmsg = format!(
                                "failed to generate a root for block#{number} since {err:?}"
                            );
                            return Err(errmsg);
                        }
                    }
                };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L199-205)
```rust
        let last_n_blocks: u64 = self.message.last_n_blocks().into();

        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L291-297)
```rust
        let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
            <= last_n_blocks
        {
            // There is not enough blocks, so we take all of them; so there is no sampled blocks.
            let sampled_numbers = Vec::new();
            let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
            (sampled_numbers, last_n_numbers)
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
