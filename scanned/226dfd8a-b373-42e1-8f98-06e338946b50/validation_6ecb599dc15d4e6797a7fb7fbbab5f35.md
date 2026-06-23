### Title
Integer Overflow in `GetLastStateProofProcess::execute` Guard Bypasses `GET_LAST_STATE_PROOF_LIMIT`, Enabling Remote DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The size-guard in `GetLastStateProofProcess::execute` uses `(last_n_blocks as usize) * 2` without overflow protection. In a release build, an attacker can supply `last_n_blocks = 2^63` (a valid `u64`) to make the multiplication wrap to `0`, bypassing the `GET_LAST_STATE_PROOF_LIMIT = 1000` check entirely. The server then proceeds to collect and process every block from `start_block_number` to the chain tip, performing expensive MMR and DB operations for each one.

---

### Finding Description

`last_n_blocks` is decoded from the peer message as a `u64`: [1](#0-0) 

The guard is: [2](#0-1) 

On a 64-bit host, `usize` is 64 bits. With `last_n_blocks = 2^63`:

```
(2^63 as usize) * 2  ==  2^64  ==  0  (wraps in release mode)
```

With `difficulties.len() = 0`, the check becomes `0 + 0 > 1000 → false`. The guard is silently bypassed.

After the guard, the code checks whether the chain range fits within `last_n_blocks`: [3](#0-2) 

Because `2^63` exceeds any realistic chain length, this branch is always taken, and `last_n_numbers` becomes the entire range `(start_block_number..last_block_number)`. For each block, `complete_headers` performs `get_ancestor`, `get_block`, and `chain_root_mmr` — all expensive storage operations: [4](#0-3) 

The constant being bypassed: [5](#0-4) 

---

### Impact Explanation

An unprivileged remote light-client peer can force the CKB node to perform O(chain_length) MMR root computations and DB lookups per single `GetLastStateProof` message. With a chain of millions of blocks and `start_block_number = 0`, this saturates I/O and CPU, causing node congestion or unresponsiveness. The attack is repeatable with no rate limit at the guard level.

---

### Likelihood Explanation

Any peer speaking the light-client protocol can send this message. No authentication, stake, or PoW is required. The overflow value (`2^63`) is a single field in a molecule-encoded message. The bypass is deterministic in release builds (Rust's default overflow behavior in release mode is wrapping).

---

### Recommendation

Replace the unchecked arithmetic with overflow-safe operations before the comparison:

```rust
let total_samples = (last_n_blocks as usize)
    .checked_mul(2)
    .and_then(|v| v.checked_add(self.message.difficulties().len()))
    .unwrap_or(usize::MAX);

if total_samples > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Alternatively, reject any `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT / 2` before the multiplication.

---

### Proof of Concept

```rust
// Pseudocode: craft a GetLastStateProof message
let msg = GetLastStateProof {
    last_n_blocks: (usize::MAX / 2 + 1) as u64,  // = 2^63
    difficulties: vec![],                          // len = 0
    last_hash: <valid tip hash>,
    start_hash: <genesis hash>,
    start_number: 0,
    difficulty_boundary: U256::max_value(),
};
// In release mode:
// (2^63 as usize) * 2 = 0  (wraps)
// 0 + 0 > 1000 → false → guard bypassed
// Server collects (0..tip_block_number) and calls complete_headers for every block
```

A unit test asserting that the limit check fires for `last_n_blocks = 2^63` would fail in a release build, confirming the bypass.

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L199-199)
```rust
        let last_n_blocks: u64 = self.message.last_n_blocks().into();
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-205)
```rust
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
