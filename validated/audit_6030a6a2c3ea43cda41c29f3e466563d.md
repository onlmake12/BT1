The overflow is real and exploitable. Let me verify the exact arithmetic and downstream code path.

The check at line 201:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
```

With `last_n_blocks = 0x8000000000000001u64` on a 64-bit target:
- `last_n_blocks as usize` = `0x8000000000000001usize` (lossless, same width)
- `0x8000000000000001usize * 2` wraps to `2` in release mode (Rust wraps on overflow in release, no panic)
- `0 + 2 > 1000` → **false** → guard passes

Then at line 291–296, since `last_n_blocks` is astronomically large, `last_block_number - start_block_number <= last_n_blocks` is always true, so:

```rust
let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
```

This allocates a vector of every block number in the chain. `complete_headers` then performs a DB read + MMR root computation for each entry.

---

### Title
Integer overflow in `GET_LAST_STATE_PROOF_LIMIT` guard bypasses work bound, enabling O(chain_length) DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary
The limit check in `GetLastStateProofProcess::execute` multiplies `last_n_blocks as usize` by 2 using plain Rust arithmetic. In release mode, this multiplication wraps on overflow, allowing an attacker to supply a `last_n_blocks` value that passes the `> 1000` guard while causing the server to allocate and process every block in the chain.

### Finding Description
In `GetLastStateProofProcess::execute`: [1](#0-0) 

`last_n_blocks` is a `u64` read directly from the peer message. The cast `last_n_blocks as usize` is lossless on 64-bit targets. The subsequent `* 2` is a plain Rust multiplication. In release builds, Rust integer overflow wraps (two's complement); there is no panic and no `checked_mul` or `saturating_mul` used here.

With `last_n_blocks = 0x8000000000000001u64`:
- `(0x8000000000000001usize) * 2 = 0x0000000000000002usize` (wraps)
- Guard: `0 + 2 > 1000` → false → execution continues

Downstream, the branch at line 291 checks `last_block_number - start_block_number <= last_n_blocks`: [2](#0-1) 

Since `last_n_blocks` is ~9.2×10¹⁸, this condition is always true for any real chain, so `last_n_numbers` collects **all** block numbers from `start_block_number` to `last_block_number`. `complete_headers` then performs a DB lookup and MMR root computation for every entry: [3](#0-2) 

The constant is defined as: [4](#0-3) 

### Impact Explanation
A single unauthenticated P2P peer can send one `GetLastStateProof` message and force the server to allocate a `Vec<BlockNumber>` proportional to the full chain length, then perform O(chain_length) DB reads and MMR computations. On a chain with millions of blocks this causes severe CPU/memory pressure. Repeated requests cause sustained degradation or OOM, affecting all light-client-serving nodes.

### Likelihood Explanation
Any peer on the P2P network can send this message. No authentication, PoW, or stake is required. The crafted value is a single field in the flatbuffers message. The overflow is deterministic and reproducible on any 64-bit release build.

### Recommendation
Replace the plain multiplication with overflow-safe arithmetic:

```rust
// Use saturating_mul so large values clamp to usize::MAX, always exceeding the limit
if self.message.difficulties().len()
    .saturating_add((last_n_blocks as usize).saturating_mul(2))
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Alternatively, reject any `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT as u64` before the cast.

### Proof of Concept
```rust
// Pseudo unit test
let last_n_blocks: u64 = 0x8000000000000001u64;
// Simulates the guard in release mode
let check_value = (last_n_blocks as usize).wrapping_mul(2); // = 2
assert!(check_value <= 1000); // passes — guard is bypassed
// Server then calls (0..chain_length).collect() unbounded
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L132-163)
```rust
        for number in numbers {
            if let Some(ancestor_header) = self.snapshot.get_ancestor(last_hash, *number) {
                let position = leaf_index_to_pos(*number);
                positions.push(position);

                let ancestor_block = self
                    .snapshot
                    .get_block(&ancestor_header.hash())
                    .ok_or_else(|| {
                        format!(
                            "failed to find block for header#{} (hash: {:#x})",
                            number,
                            ancestor_header.hash()
                        )
                    })?;
                let uncles_hash = ancestor_block.calc_uncles_hash();
                let extension = ancestor_block.extension();

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
