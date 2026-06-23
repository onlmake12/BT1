Looking at the actual code carefully to verify the overflow claim and its downstream effects.

### Title
Integer Overflow in `GetLastStateProofProcess::execute` Bypasses Limit Check, Enabling Full-Chain Traversal DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

An unprivileged remote peer can send a crafted `GetLastStateProof` P2P message with `last_n_blocks` set to `2^63` (a valid `u64` value). In Rust release mode, the guard expression `(last_n_blocks as usize) * 2` wraps to `0`, causing the limit check to evaluate to `false` despite `last_n_blocks` being astronomically large. Downstream, the server collects the entire chain into a `Vec` and calls `complete_headers` on every block, causing an unbounded traversal that can hang or OOM-crash the light-client server.

---

### Finding Description

**Root cause — wrapping multiplication at the guard:** [1](#0-0) 

```rust
let last_n_blocks: u64 = self.message.last_n_blocks().into();

if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT          // = 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

`last_n_blocks` is a `Uint64` wire field: [2](#0-1) 

On a 64-bit target `usize` is also 64 bits. In Rust **release mode** integer arithmetic wraps silently. With `last_n_blocks = 2^63`:

```
(2^63 as usize) * 2  ==  2^64 mod 2^64  ==  0
```

So `difficulties.len() + 0 > 1000` is `false` for any normally-sized `difficulties` list, and the guard returns without rejecting the message.

**Downstream — full-chain range collected:** [3](#0-2) 

```rust
let reorg_last_n_numbers = if start_block_number == 0
    || snapshot.get_ancestor(&last_block_hash, start_block_number)
        .map(|header| header.hash() == start_block_hash)
        .unwrap_or(false)
{
    Vec::new()
} else {
    let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
    (min_block_number..start_block_number).collect()   // ← unbounded
};
```

If the attacker supplies a valid `last_hash` (any real main-chain tip hash) but a deliberately wrong `start_hash`, the `else` branch executes. With `last_n_blocks = 2^63` and `start_block_number = T` (current tip):

```
min(T, 2^63) = T          (chain is far shorter than 2^63)
min_block_number = T - T = 0
reorg_last_n_numbers = (0..T).collect()   // entire chain
```

**Unbounded work in `complete_headers`:** [4](#0-3) 

`block_numbers` (which now contains every block from 0 to the tip) is passed to `complete_headers`, which calls `snapshot.get_ancestor` and `snapshot.get_block` for every entry — O(N) expensive store lookups for a chain of N blocks.

---

### Impact Explanation

- **Memory:** `(0..T).collect::<Vec<_>>()` allocates a `Vec<u64>` with T entries. At ~10 M blocks that is ~80 MB just for the numbers; the subsequent `Vec<packed::VerifiableHeader>` is far larger.
- **CPU / latency:** Each `get_ancestor` call traverses the skip-list / MMR; for millions of blocks this is effectively unbounded work on the server's async task.
- **Result:** The light-client server thread hangs for the duration of the traversal, or the process is killed by OOM, denying service to all legitimate light clients.

---

### Likelihood Explanation

- The field is freely settable by any peer; no PoW, no key, no privilege required.
- The value `2^63` is a single 8-byte little-endian integer in the molecule-encoded message — trivial to craft.
- The only prerequisite is knowing one valid main-chain block hash, which is public.
- The attack is repeatable: a single persistent peer can re-send the message continuously.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant before the comparison:

```rust
// Option A – saturating_mul (never wraps, always ≥ true value)
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}

// Option B – reject immediately if last_n_blocks alone exceeds the limit
if last_n_blocks as usize > constant::GET_LAST_STATE_PROOF_LIMIT / 2 {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Either change closes the bypass. Option B is simpler and avoids any arithmetic on the untrusted value.

---

### Proof of Concept

```rust
// Craft the message fields:
let last_n_blocks: u64 = (usize::MAX / 2 + 1) as u64;  // = 2^63 on 64-bit
// Verify the overflow:
assert_eq!((last_n_blocks as usize).wrapping_mul(2), 0);

// Message:
//   last_hash    = <any valid main-chain tip hash>
//   start_hash   = [0u8; 32]          (wrong hash → triggers else branch)
//   start_number = <current tip number T>
//   last_n_blocks = 2^63
//   difficulties  = []                (empty → skips difficulty checks)
//   difficulty_boundary = U256::MAX

// Expected server behaviour (release build):
//   1. Guard: 0 + 0 > 1000 → false  → passes
//   2. is_main_chain(last_hash) → true
//   3. start_block_number (T) ≤ last_block_number (T) → passes
//   4. get_ancestor mismatch → else branch
//   5. reorg_last_n_numbers = (0..T).collect()  ← entire chain
//   6. complete_headers iterates all T blocks → OOM / hang
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L199-205)
```rust
        let last_n_blocks: u64 = self.message.last_n_blocks().into();

        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L237-247)
```rust
        let reorg_last_n_numbers = if start_block_number == 0
            || snapshot
                .get_ancestor(&last_block_hash, start_block_number)
                .map(|header| header.hash() == start_block_hash)
                .unwrap_or(false)
        {
            Vec::new()
        } else {
            let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
            (min_block_number..start_block_number).collect()
        };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L350-365)
```rust
        let block_numbers = reorg_last_n_numbers
            .into_iter()
            .chain(sampled_numbers)
            .chain(last_n_numbers)
            .collect::<Vec<_>>();

        let (positions, headers) = {
            let mut positions: Vec<u64> = Vec::new();
            let headers =
                match sampler.complete_headers(&mut positions, &last_block_hash, &block_numbers) {
                    Ok(headers) => headers,
                    Err(errmsg) => {
                        return StatusCode::InternalError.with_context(errmsg);
                    }
                };
            (positions, headers)
```

**File:** util/gen-types/schemas/extensions.mol (L336-336)
```text
    last_n_blocks:              Uint64,
```
