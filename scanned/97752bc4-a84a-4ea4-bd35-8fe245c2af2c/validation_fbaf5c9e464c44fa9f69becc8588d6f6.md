The overflow is real. Let me trace the full path precisely.

### Title
Integer Overflow in `GetLastStateProofProcess::execute` Bypasses Limit Check, Enabling Full-Chain Traversal DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

An unprivileged remote peer can send a `GetLastStateProof` message with `last_n_blocks` set to a value that causes `(last_n_blocks as usize) * 2` to wrap to `0` in Rust release-mode wrapping arithmetic. This makes the guard check evaluate to `false`, allowing the server to proceed and collect every block from `start_block_number` to `last_block_number` — potentially the entire chain — before passing them all to `complete_headers`, which performs multiple expensive DB lookups and MMR root computations per block.

---

### Finding Description

**Step 1 — Attacker-controlled field decoded as `u64`:** [1](#0-0) 

`last_n_blocks` is a `u64` decoded directly from the P2P message with no prior bounds check.

**Step 2 — Overflow in the limit guard:** [2](#0-1) 

In Rust release mode, `(last_n_blocks as usize) * 2` uses wrapping arithmetic. On a 64-bit target, `usize` is 64 bits, so setting `last_n_blocks = 2^63` makes:

```
(2^63_usize) * 2  →  0   (wraps)
```

The guard becomes `difficulties.len() + 0 > 1000`, which is `false` when `difficulties` is empty. The function proceeds past the only size check.

**Step 3 — Full-chain collection:** [3](#0-2) 

With `last_n_blocks = 2^63`, the condition `last_block_number - start_block_number <= last_n_blocks` is always true for any real chain height. The server collects every block number from `start_block_number` to `last_block_number` into `last_n_numbers`. With attacker-supplied `start_block_number = 0`, this is the entire chain.

**Step 4 — Expensive per-block work in `complete_headers`:** [4](#0-3) 

For every block number in the collected set, `complete_headers` calls `snapshot.get_ancestor()`, `snapshot.get_block()`, `calc_uncles_hash()`, and `mmr.get_root()`. On a chain with millions of blocks, this is millions of DB reads and MMR computations triggered by a single P2P message.

**The constant being bypassed:** [5](#0-4) 

`GET_LAST_STATE_PROOF_LIMIT = 1000` is the only guard; there is no secondary cap on `block_numbers` after the overflow.

---

### Impact Explanation

A single malicious peer can force the light-client server to allocate a `Vec` of millions of block numbers, then perform O(chain_height) DB reads and MMR root computations synchronously. Repeated requests exhaust CPU and memory, hanging or crashing the light-client server process. No PoW, no stake, and no privileged role is required.

---

### Likelihood Explanation

The exploit requires only crafting a single valid-looking `GetLastStateProof` message with `last_n_blocks = 2^63` (or any value where `(v as usize)*2` wraps below 1000), a real tip hash (publicly observable), and `start_number = 0`. This is trivially constructable by any peer.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant before the comparison:

```rust
// Option A: saturating_mul prevents wrap-around
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Or, reject `last_n_blocks` values that exceed the limit before any arithmetic:

```rust
if last_n_blocks as usize > constant::GET_LAST_STATE_PROOF_LIMIT / 2 {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, add a hard cap on `block_numbers.len()` after assembly (line 350–354) as a defense-in-depth measure.

---

### Proof of Concept

```rust
// Craft message fields:
// last_n_blocks  = (usize::MAX / 2) + 1  = 0x8000_0000_0000_0000_u64
// difficulties   = []   (empty)
// start_number   = 0
// last_hash      = current mainnet tip hash (public)
// start_hash     = any hash != ancestor of last_hash at 0 (triggers reorg path)

// In release mode:
let last_n_blocks: u64 = 0x8000_0000_0000_0000;
let check = 0_usize + (last_n_blocks as usize) * 2;  // = 0 (wraps)
assert!(check <= 1000);  // passes — guard bypassed

// Server then executes:
// last_n_numbers = (0..last_block_number)  // entire chain
// complete_headers iterates every block → millions of DB reads
```

Fuzz `last_n_blocks` with values in `[usize::MAX/2, usize::MAX]` in a release build and assert that `block_numbers.len()` never exceeds `GET_LAST_STATE_PROOF_LIMIT` — the assertion will fire.

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L132-135)
```rust
        for number in numbers {
            if let Some(ancestor_header) = self.snapshot.get_ancestor(last_hash, *number) {
                let position = leaf_index_to_pos(*number);
                positions.push(position);
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
