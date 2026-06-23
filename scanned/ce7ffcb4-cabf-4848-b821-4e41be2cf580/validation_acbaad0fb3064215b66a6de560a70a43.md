Let me verify the exact arithmetic behavior and the packed message field type.

### Title
Unbounded `reorg_last_n_numbers` Allocation via `usize` Overflow in Limit Check — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

`GetLastStateProofProcess::execute` contains a guard intended to cap per-request work at `GET_LAST_STATE_PROOF_LIMIT = 1000`. The guard expression `(last_n_blocks as usize) * 2` overflows `usize` in a release build when `last_n_blocks >= 2^63`, wrapping to a small value and silently bypassing the check. After the guard, the reorg path allocates `(0..start_block_number)` — a Vec whose length is bounded only by the actual chain height — before any further validation.

---

### Finding Description

**Guard expression (line 201):**

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) 

`last_n_blocks` is decoded from the wire as a full `Uint64` field:

```
last_n_blocks: Uint64
``` [2](#0-1) 

On a 64-bit host, `usize` is 64 bits. With `last_n_blocks = 2^63` (`u64::MAX/2 + 1`):

- `(2^63_u64 as usize) = 2^63` — fits in `usize`
- `2^63 * 2 = 2^64` — overflows `usize` (max = `2^64 − 1`)

In a Rust **release build**, integer overflow wraps (two's complement), producing `0`. The guard becomes `0 + 0 > 1000` → `false`. The check is silently bypassed.

**Reorg path (lines 237–247):**

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
``` [3](#0-2) 

With `last_n_blocks = 2^63` and any realistic `start_block_number = N` (where `N << 2^63`):

- `min(N, 2^63) = N`
- `min_block_number = N − N = 0`
- `(0..N).collect::<Vec<u64>>()` allocates **N elements** — the entire chain from genesis to `start_block_number`

The only prior bound on `start_block_number` is that it must not exceed `last_block_number`: [4](#0-3) 

On CKB mainnet (~14 M blocks), this yields a ~112 MB allocation per request. The code then passes `reorg_last_n_numbers` into `complete_headers`, which performs one `get_ancestor` DB lookup per element — millions of synchronous lookups per request. [5](#0-4) 

---

### Impact Explanation

A single crafted `GetLastStateProof` P2P message causes the server to:
1. Allocate a `Vec<u64>` of up to `chain_height` elements (hundreds of MB on mainnet).
2. Perform `chain_height` sequential `get_ancestor` DB lookups.

Repeated requests from one or more peers exhaust heap memory (OOM kill) or saturate the async executor, causing the light-client protocol server to crash or become unresponsive. The impact is scoped to the light-client protocol server process.

---

### Likelihood Explanation

The attack requires only:
1. A valid `last_hash` — trivially obtained from any public node or block explorer.
2. `start_block_number` set to a large value ≤ chain tip — any valid block number works.
3. `start_hash` set to any value that does not match the ancestor at `start_block_number` — a random 32-byte value suffices.
4. `last_n_blocks = 2^63` — a single field in the packed message.

No authentication, no PoW, no privileged role. Any peer that can open a light-client protocol connection can trigger this.

---

### Recommendation

Replace the overflowing expression with a saturating or checked cast before multiplication:

```rust
// Option A: saturating_mul prevents wrap-around
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, add an explicit cap on `last_n_blocks` itself before the reorg path:

```rust
if last_n_blocks as usize > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("last_n_blocks too large");
}
```

This ensures `reorg_last_n_numbers` is always bounded by `GET_LAST_STATE_PROOF_LIMIT` regardless of the reorg condition.

---

### Proof of Concept

```rust
// Craft a message with:
//   last_hash    = current tip hash (valid main-chain block)
//   start_hash   = [0u8; 32]  (wrong hash → triggers reorg path)
//   start_number = tip_number (large value, e.g. 14_000_000 on mainnet)
//   last_n_blocks = (u64::MAX / 2) + 1  // = 2^63; causes (usize)*2 to wrap to 0
//   difficulty_boundary = any valid value
//   difficulties = []

let content = packed::GetLastStateProof::new_builder()
    .last_hash(tip_hash)
    .start_hash(packed::Byte32::default())   // wrong hash → reorg branch
    .start_number(tip_number.pack())
    .last_n_blocks(((u64::MAX / 2) + 1).pack())  // overflow trigger
    .difficulty_boundary(U256::zero().pack())
    .build();
```

**Expected (correct) behavior:** server rejects with `MalformedProtocolMessage` before any allocation.

**Actual behavior (release build):** guard passes (`0 > 1000` is false), server allocates `(0..tip_number).collect::<Vec<u64>>()` — up to hundreds of MB — then iterates over it performing millions of DB lookups. [6](#0-5)

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L231-235)
```rust
        if start_block_number > last_block_number {
            return StatusCode::InvalidRequest.with_context(format!(
                "the start block number ({start_block_number}) should not be greater than the last block number ({last_block_number})"
            ));
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

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
