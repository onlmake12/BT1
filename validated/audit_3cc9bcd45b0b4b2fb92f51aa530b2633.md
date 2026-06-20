### Title
Integer Overflow in `(last_n_blocks as usize) * 2` Bypasses Limit Check, Enabling Full Chain Traversal DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

In Rust release builds, the multiplication `(last_n_blocks as usize) * 2` at line 201 wraps silently on overflow. A remote peer can send a `GetLastStateProof` message with `last_n_blocks` set to `(usize::MAX / 2) + 1`, causing the product to wrap to `0`, bypassing the `GET_LAST_STATE_PROOF_LIMIT` guard entirely. The server then performs full-chain traversal in `complete_headers`, causing a DoS.

---

### Finding Description

**Attacker-controlled input:** The `last_n_blocks` field is a peer-supplied `u64` deserialized from the molecule wire format with no prior bounds check. [1](#0-0) 

The guard at line 201 is:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // = 1000
```

In a Rust **release build**, integer arithmetic on primitive types wraps on overflow (no panic, no abort). On a 64-bit target, `usize` is 64 bits. If the attacker sends:

```
last_n_blocks = (usize::MAX / 2) + 1  =  9_223_372_036_854_775_808  (0x8000_0000_0000_0000)
```

then:

```
(last_n_blocks as usize) * 2  =  0x8000_0000_0000_0000 * 2  =  0  (wraps mod 2^64)
```

The check becomes `difficulties.len() + 0 > 1000`, which is `false` for any normally-sized request, so execution continues past the guard. [2](#0-1) 

**Post-bypass execution paths:**

1. **Reorg path (line 245):** `min(start_block_number, last_n_blocks)` uses `u64` arithmetic — no overflow here. With a huge `last_n_blocks`, `min(...)` equals `start_block_number`, so `min_block_number = 0` and `reorg_last_n_numbers = (0..start_block_number)` — the entire chain up to the start block. This path is triggered whenever the attacker supplies a `start_hash` that does not match the canonical ancestor at `start_block_number`. [3](#0-2) 

2. **Last-N path (line 291):** `last_block_number - start_block_number <= last_n_blocks` is always `true` when `last_n_blocks` is near `u64::MAX/2`, so `last_n_numbers = (start_block_number..last_block_number)` — the entire chain from start to tip. [4](#0-3) 

3. **Combined:** `block_numbers` is the union of both ranges, potentially covering the entire chain. `complete_headers` then calls `snapshot.get_ancestor()` and `snapshot.get_block()` for every entry — millions of expensive DB lookups. [5](#0-4) 

There is no `checked_mul`, `saturating_mul`, or any other overflow-safe arithmetic used anywhere in this file.

---

### Impact Explanation

A single malicious peer message causes the light-client server to traverse the entire chain in `complete_headers`, performing O(chain_height) disk reads. On mainnet (millions of blocks), this hangs or crashes the light-client server thread, denying service to all legitimate light clients. The attacker needs no credentials, no PoW, and no special peer status.

---

### Likelihood Explanation

The exploit is trivially constructable: craft a molecule-encoded `GetLastStateProof` message with `last_n_blocks = 0x8000000000000000`, a valid `last_hash` on the main chain, `start_number = 0`, and an empty `difficulties` list. All production CKB binaries are compiled in release mode. No fuzzing is required — the exact trigger value is analytically derivable.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant:

```rust
// Before (vulnerable in release mode):
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT

// After:
let last_n_blocks_x2 = (last_n_blocks as usize).saturating_mul(2);
if self.message.difficulties().len().saturating_add(last_n_blocks_x2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
```

Alternatively, reject any `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT / 2` immediately after parsing.

---

### Proof of Concept

```rust
// On a 64-bit release build:
let last_n_blocks: u64 = (usize::MAX as u64 / 2) + 1; // 0x8000_0000_0000_0000
let product = (last_n_blocks as usize) * 2;            // wraps to 0
assert_eq!(product, 0);                                // passes in release mode

// Limit check evaluates to:
// 0 (difficulties) + 0 > 1000  =>  false  =>  guard bypassed
// Then: reorg_last_n_numbers = (0..start_block_number) = entire chain
```

Send via P2P: `GetLastStateProof { last_n_blocks: 0x8000000000000000, last_hash: <tip_hash>, start_hash: <any_non_canonical_hash>, start_number: <chain_tip>, difficulties: [], difficulty_boundary: <max_u256> }`.

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

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
