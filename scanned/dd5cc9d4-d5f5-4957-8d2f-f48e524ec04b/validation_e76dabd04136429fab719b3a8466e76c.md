### Title
Integer Overflow in `GET_LAST_STATE_PROOF_LIMIT` Guard Enables Unbounded Heap Allocation via Crafted `GetLastStateProof` Message — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The rate-limiting guard in `GetLastStateProofProcess::execute` computes `(last_n_blocks as usize) * 2` without overflow protection. In Rust release mode, integer arithmetic wraps silently. An attacker can supply `last_n_blocks = 2^63` (a valid `Uint64` wire value), causing the multiplication to wrap to `0`, bypassing the `GET_LAST_STATE_PROOF_LIMIT = 1000` check entirely. The server then collects every block number from `start_block_number` to `last_block_number` into a `Vec`, and subsequently calls `complete_headers` on all of them — performing millions of DB lookups and building millions of `VerifiableHeader` structs — exhausting heap memory and/or CPU/IO, crashing or hanging the node.

---

### Finding Description

**Root cause — the overflowing guard:** [1](#0-0) 

`last_n_blocks` is decoded as a plain `u64` from the wire message. On a 64-bit host `usize` is also 64 bits, so `last_n_blocks as usize` is a no-op cast. The expression `(last_n_blocks as usize) * 2` then overflows in Rust release mode (wrapping semantics) when `last_n_blocks >= 2^63`. With `last_n_blocks = 2^63` the product wraps to exactly `0`, and `0 + 0 > 1000` is `false`, so the guard returns without banning the peer.

**Protocol schema confirms the field is a full `Uint64`:** [2](#0-1) 

Any value in `[0, 2^64)` is a valid wire encoding; no schema-level bound exists.

**Downstream unbounded allocation — branch taken when `last_n_blocks` is huge:** [3](#0-2) 

With `last_n_blocks = 2^63` and `start_block_number = 0`, the condition `last_block_number - 0 <= 2^63` is trivially true for any realistic chain height (mainnet ~12 M blocks). The server then executes `(0..last_block_number).collect::<Vec<_>>()`, allocating a `Vec<u64>` of ~12 M entries (~96 MB).

**Subsequent per-block work amplifies the damage:** [4](#0-3) 

`complete_headers` is called with all ~12 M block numbers. For each entry it performs:
- `snapshot.get_ancestor` (DB read)
- `snapshot.get_block` (DB read)
- `snapshot.chain_root_mmr(*number - 1).get_root()` (MMR computation)
- Construction of a `VerifiableHeader` struct [5](#0-4) 

Each `VerifiableHeader` contains a full block header, uncles hash, optional extension, and a `HeaderDigest`. At ~300+ bytes per struct, 12 M entries ≈ 3.6 GB of heap allocation, far exceeding typical server RAM.

**The constant being guarded:** [6](#0-5) 

---

### Impact Explanation

A single crafted `GetLastStateProof` P2P message causes the full-node process to attempt allocating several gigabytes of heap and performing millions of RocksDB reads. The process either OOMs and is killed by the OS, or becomes unresponsive for minutes, taking the node off the network. No authentication, PoW, or privileged role is required — any peer that can open a light-client protocol connection can trigger this.

---

### Likelihood Explanation

The light-client protocol is enabled on production CKB nodes that serve light clients. The attack requires only a TCP connection and a single ~100-byte message. The overflow value (`2^63`) is trivial to compute. There is no rate-limiting, connection authentication, or secondary guard that would stop this before the allocation occurs.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant before comparing against the limit:

```rust
// Before (vulnerable — wraps in release mode):
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT

// After (safe):
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
```

Alternatively, reject the message immediately if `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT as u64 / 2` before any cast.

---

### Proof of Concept

```
Preconditions:
  - Server has a chain of N blocks (N large, e.g., mainnet ~12 M)
  - Attacker connects as a light-client peer
  - last_hash = valid main-chain tip hash
  - start_block_number = 0, start_hash = genesis hash
  - difficulties = [] (empty)
  - last_n_blocks = 9223372036854775808  (= 2^63 = usize::MAX/2 + 1)

Step 1: Attacker sends LightClientMessage::GetLastStateProof with the above fields.

Step 2: Server calls GetLastStateProofProcess::execute.
  - last_n_blocks as usize = 9223372036854775808
  - (9223372036854775808_usize) * 2 = 0  (wrapping overflow, release mode)
  - 0 + 0 > 1000  →  false  →  guard NOT triggered

Step 3: last_hash is on main chain → proceeds past is_main_chain check.

Step 4: start_block_number (0) <= last_block_number (~12M) → passes.

Step 5: start_block_number == 0 → reorg_last_n_numbers = [].

Step 6: last_block_number - 0 = ~12M <= 2^63 → true
  → last_n_numbers = (0..12_000_000).collect()  [96 MB Vec]

Step 7: complete_headers called with 12M block numbers
  → 12M × (2 DB reads + MMR root + VerifiableHeader alloc)
  → ~3.6 GB heap allocation + millions of RocksDB reads
  → OOM kill or prolonged unresponsiveness
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L124-180)
```rust
    fn complete_headers(
        &self,
        positions: &mut Vec<u64>,
        last_hash: &packed::Byte32,
        numbers: &[BlockNumber],
    ) -> Result<Vec<packed::VerifiableHeader>, String> {
        let mut headers = Vec::new();

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

                let header = packed::VerifiableHeader::new_builder()
                    .header(ancestor_header.data())
                    .uncles_hash(uncles_hash)
                    .extension(Pack::pack(&extension))
                    .parent_chain_root(parent_chain_root)
                    .build();

                headers.push(header);
            } else {
                let errmsg = format!("failed to find ancestor header ({number})");
                return Err(errmsg);
            }
        }

        Ok(headers)
    }
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L356-365)
```rust
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
