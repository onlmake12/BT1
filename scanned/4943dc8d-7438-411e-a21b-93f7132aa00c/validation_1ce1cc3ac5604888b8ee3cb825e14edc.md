### Title
Integer Overflow in `GetLastStateProofProcess::execute` Bypasses `GET_LAST_STATE_PROOF_LIMIT` Guard — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The limit check at line 201 computes `(last_n_blocks as usize) * 2` without overflow protection. In a release build, a peer supplying `last_n_blocks = 2^63` causes this multiplication to wrap to `0`, making the guard trivially false. The server then iterates over the entire chain to build `last_n_numbers`, performing O(chain_height) DB reads and MMR root computations per message with no peer ban.

---

### Finding Description

In `GetLastStateProofProcess::execute`:

```rust
// line 199
let last_n_blocks: u64 = self.message.last_n_blocks().into();

// line 201-205
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) 

On a 64-bit target, `usize` is 64 bits. With `last_n_blocks = 2^63`:
- `2^63 as usize` = `9223372036854775808` (no truncation)
- `9223372036854775808 * 2` = `2^64` → wraps to **`0`** in Rust release builds (debug builds panic)

With `difficulties = []`, the expression evaluates to `0 + 0 = 0 > 1000` → **false**. The guard is bypassed entirely.

After the bypass, the code at lines 291–297 evaluates:

```rust
if last_block_number - start_block_number <= last_n_blocks {
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
``` [2](#0-1) 

Since `last_n_blocks = 2^63` dwarfs any real chain height, this branch is always taken, and `last_n_numbers` collects **every block from `start_block_number` to tip**. `complete_headers` then iterates over all of them, calling `get_ancestor`, `get_block`, and `chain_root_mmr(...).get_root()` per entry: [3](#0-2) 

No `checked_mul`, `saturating_mul`, or any other overflow guard exists anywhere in this path. [4](#0-3) 

---

### Impact Explanation

A single unauthenticated light-client peer can force O(chain_height) DB reads and MMR root computations per `GetLastStateProof` message. On a chain of height 10,000 this is 10,000 DB lookups and 10,000 MMR root computations per message, with no rate limiting, no peer ban, and no back-pressure. Repeated at high frequency this causes sustained CPU and I/O saturation on the server node. Impact is **High (10001–15000)** as stated in scope.

---

### Likelihood Explanation

- The attacker needs only an open LightClient P2P session — no credentials, no PoW, no stake.
- The overflow is deterministic and reproducible on any 64-bit release build.
- The exploit payload is a single small message with one field set to `2^63`.
- No existing validation in the function prevents this path.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant:

```rust
// Before (vulnerable):
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT

// After (safe):
let n = (last_n_blocks as usize).saturating_mul(2);
if self.message.difficulties().len().saturating_add(n)
    > constant::GET_LAST_STATE_PROOF_LIMIT
```

This ensures any value of `last_n_blocks ≥ 500` immediately saturates to `usize::MAX` and triggers the rejection branch.

---

### Proof of Concept

```rust
// Pseudocode unit test
let chain_height = 10_000u64;
// build mock chain of height 10_000
let last_n_blocks: u64 = 1u64 << 63; // 2^63
// (last_n_blocks as usize) * 2 == 0 in release build
assert_eq!((last_n_blocks as usize).wrapping_mul(2), 0);
// guard: 0 + 0 > 1000 == false → bypassed
// last_n_numbers = (0..10_000) → 10_000 entries
// complete_headers called with 10_000 block numbers
// assert complete_headers.len() > GET_LAST_STATE_PROOF_LIMIT (1000)
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
