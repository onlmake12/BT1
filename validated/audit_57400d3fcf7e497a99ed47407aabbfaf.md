### Title
Integer Overflow in `GetLastStateProof` Guard Bypasses Sample Limit, Enabling O(chain_length) DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

An unprivileged remote peer can craft a `GetLastStateProof` message with `last_n_blocks` set to any value ≥ `usize::MAX/2 + 1` (e.g., `0x8000000000000001` on 64-bit). In Rust release builds, the expression `(last_n_blocks as usize) * 2` wraps to a small value (e.g., `2`), silently bypassing the `GET_LAST_STATE_PROOF_LIMIT = 1000` guard. The raw `last_n_blocks` value is then used unchanged in downstream range computations, forcing the server to allocate and process O(chain_length) block numbers and compute one MMR root per block inside `complete_headers`.

---

### Finding Description

**Overflow site — line 201:**

```rust
let last_n_blocks: u64 = self.message.last_n_blocks().into();

if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT          // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) 

With `last_n_blocks = 0x8000000000000001_u64` on a 64-bit host:
- `last_n_blocks as usize` = `0x8000000000000001`
- `* 2` wraps (release mode, no overflow check) → `0x0000000000000002 = 2`
- `0 + 2 > 1000` → **false** → guard does not fire

**Downstream use of the raw (huge) value — `reorg_last_n_numbers`:**

```rust
let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
(min_block_number..start_block_number).collect()   // up to chain_length entries
``` [2](#0-1) 

Because `min(start_block_number, 0x8000000000000001) = start_block_number`, `min_block_number` collapses to `0`, and the range `(0..start_block_number)` collects every block number from genesis to the requested start — O(chain_length) entries.

**Downstream use — `last_n_numbers`:**

```rust
if last_block_number - start_block_number <= last_n_blocks {
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
``` [3](#0-2) 

The condition is always true for any real chain (chain length ≪ `0x8000000000000001`), so `last_n_numbers` also collects O(chain_length) entries.

**Per-entry cost in `complete_headers`:**

For every collected block number, `complete_headers` calls `self.snapshot.chain_root_mmr(*number - 1).get_root()` — a non-trivial MMR root computation. [4](#0-3) 

The limit constant itself: [5](#0-4) 

---

### Impact Explanation

A single malicious peer can force the full node to perform O(chain_length) MMR root computations and O(chain_length) `get_ancestor` + `get_block` store lookups per request. On a mainnet node with millions of blocks, this saturates CPU and memory. Multiple concurrent peers amplify the effect, stalling or crashing the node and all connected light clients.

---

### Likelihood Explanation

The exploit requires only a valid P2P connection and a single crafted `GetLastStateProof` message — no PoW, no keys, no privileged role. The overflow value (`0x8000000000000001`) is trivially constructable. The path is fully reachable in production release builds where Rust wrapping arithmetic is the default.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant before the comparison:

```rust
// Option A: saturating_mul — safe, no panic
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Alternatively, reject any `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT / 2` before the multiplication is attempted, which is simpler and equally correct.

---

### Proof of Concept

```rust
// Pseudocode unit test
let last_n_blocks: u64 = 0x8000000000000001;
// Simulates release-mode wrapping:
let guard_value = (last_n_blocks as usize).wrapping_mul(2);
assert_eq!(guard_value, 2);                          // overflow → 2
assert!(0 + guard_value <= 1000);                    // guard passes — BUG

// Downstream: with start_block_number = 1_000_000 and start_hash ≠ ancestor hash
let min_block_number = 1_000_000u64 - std::cmp::min(1_000_000u64, last_n_blocks);
assert_eq!(min_block_number, 0);
let reorg_range: Vec<u64> = (min_block_number..1_000_000).collect();
assert_eq!(reorg_range.len(), 1_000_000);            // 1M MMR computations triggered
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L153-163)
```rust
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L245-246)
```rust
            let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
            (min_block_number..start_block_number).collect()
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L291-296)
```rust
        let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
            <= last_n_blocks
        {
            // There is not enough blocks, so we take all of them; so there is no sampled blocks.
            let sampled_numbers = Vec::new();
            let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
