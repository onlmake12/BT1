### Title
Integer Overflow in `GetLastStateProofProcess::execute` Guard Bypasses `GET_LAST_STATE_PROOF_LIMIT`, Enabling Unbounded DB Iteration — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The guard intended to cap server-side work at `GET_LAST_STATE_PROOF_LIMIT = 1000` uses an unchecked multiplication `(last_n_blocks as usize) * 2` that wraps to zero in Rust release mode when `last_n_blocks ≥ 2^63`. A remote, unprivileged light-client peer can exploit this to force the server to iterate over the entire chain history — one `get_ancestor` + `chain_root_mmr.get_root()` DB read per block — per single request.

---

### Finding Description

In `GetLastStateProofProcess::execute`, the very first guard is:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) 

`last_n_blocks` is a `u64` decoded directly from the peer message: [2](#0-1) 

The schema confirms `last_n_blocks` is a `Uint64` — fully attacker-controlled with no prior validation: [3](#0-2) 

On a 64-bit target, `usize` is 64 bits. The cast `last_n_blocks as usize` is a no-op. In Rust **release mode**, integer overflow wraps (two's complement); it does **not** panic. Therefore:

- `last_n_blocks = 2^63` → `(2^63 as usize) * 2 = 0` (wraps)
- Guard evaluates to `difficulties.len() + 0 > 1000` → **false** → guard is bypassed

After the guard, the `reorg_last_n_numbers` vector is computed:

```rust
let reorg_last_n_numbers = if start_block_number == 0
    || snapshot.get_ancestor(...).map(|h| h.hash() == start_block_hash).unwrap_or(false)
{
    Vec::new()
} else {
    let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
    (min_block_number..start_block_number).collect()
};
``` [4](#0-3) 

With `last_n_blocks = 2^63` and `start_block_number = N` (any value ≤ chain tip):
- `min(N, 2^63) = N` (since N is far smaller)
- `min_block_number = N - N = 0`
- `reorg_last_n_numbers = (0..N)` — **N entries, unbounded by any limit**

The `else` branch is triggered whenever the attacker sends a `start_hash` that does not match the actual ancestor at `start_block_number`, which requires no privilege — just sending an arbitrary byte32 value.

This vector is then passed directly into `complete_headers`, which performs two DB reads per entry:

```rust
for number in numbers {
    if let Some(ancestor_header) = self.snapshot.get_ancestor(last_hash, *number) {
        ...
        let mmr = self.snapshot.chain_root_mmr(*number - 1);
        match mmr.get_root() { ... }
    }
}
``` [5](#0-4) 

There is no size check on `reorg_last_n_numbers` before this loop executes.

---

### Impact Explanation

On a mainnet node with ~14 million blocks, a single crafted `GetLastStateProof` message with `last_n_blocks = 2^63`, a valid `last_hash`, `start_number = 13_999_999`, and a mismatched `start_hash` causes ~14 million sequential DB reads in a single request handler. An attacker sending this message repeatedly (or from multiple peers) causes sustained CPU and I/O exhaustion, degrading block validation, peer sync, and all other node functions. The constant `GET_LAST_STATE_PROOF_LIMIT = 1000` is the intended invariant: [6](#0-5) 

That invariant is completely defeated by the overflow.

---

### Likelihood Explanation

The attack requires only a P2P connection to the light-client protocol port — no authentication, no PoW, no stake. The crafted message is a single valid-length `GetLastStateProof` molecule-encoded packet. The overflow value (`2^63`) is a single fixed constant. Any attacker who can connect to the node's light-client port can trigger this.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant, and add an explicit upper bound on `last_n_blocks` itself before any further use:

```rust
// Option 1: reject oversized last_n_blocks directly
if last_n_blocks as usize > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("last_n_blocks too large");
}

// Option 2: use saturating arithmetic in the guard
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, add a size check on `reorg_last_n_numbers` after it is computed and before `complete_headers` is called, as defense-in-depth.

---

### Proof of Concept

```rust
// Craft the malicious message:
let content = packed::GetLastStateProof::new_builder()
    .last_hash(valid_tip_hash)           // any valid main-chain block hash
    .start_hash(Byte32::zero())          // deliberately wrong hash → triggers else branch
    .start_number(999_999u64)            // large start_number on a 1M-block chain
    .last_n_blocks(9223372036854775808u64) // 2^63: causes (as usize)*2 == 0 in release
    .difficulty_boundary(some_valid_boundary)
    // difficulties: empty → difficulties.len() == 0
    .build();
```

Guard evaluation: `0 + (9223372036854775808usize).wrapping_mul(2) = 0 > 1000` → **false** → passes.

`reorg_last_n_numbers = (0..999_999)` → 999,999 entries → 999,999 × 2 DB reads in `complete_headers`.

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

**File:** util/gen-types/schemas/extensions.mol (L336-336)
```text
    last_n_blocks:              Uint64,
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
