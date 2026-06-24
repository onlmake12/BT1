Audit Report

## Title
Integer Overflow in `GetLastStateProof` Limit Guard Enables Unbounded Allocation DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The limit guard at line 201 performs an unchecked multiplication `(last_n_blocks as usize) * 2` that wraps to zero in Rust release mode when `last_n_blocks = 2^63`, bypassing `GET_LAST_STATE_PROOF_LIMIT` entirely. An unauthenticated peer can then trigger allocations of two Vecs proportional to the full chain height and force `complete_headers` to perform millions of DB lookups and MMR root computations, exhausting process memory and crashing the node.

## Finding Description

**Overflow in the limit guard:** [1](#0-0) 

`last_n_blocks` is a `u64` cast to `usize`. On a 64-bit target `usize` is 64 bits. With `last_n_blocks = 2^63`:
- `(2^63_usize) * 2 = 2^64` — wraps to **0** in Rust release mode (no overflow checks)
- `difficulties.len() (= 0) + 0 > 1000` → **false** → guard bypassed

The limit constant being bypassed: [2](#0-1) 

**Unbounded `reorg_last_n_numbers` allocation:** [3](#0-2) 

When `start_hash` is a crafted wrong value (any 32-byte value that doesn't match the real ancestor), the reorg branch executes. With `last_n_blocks = 2^63 > start_block_number`, `min(start_block_number, last_n_blocks) = start_block_number`, so `min_block_number = 0` and the Vec spans `(0..start_block_number)` — entirely uncapped by `GET_LAST_STATE_PROOF_LIMIT`.

**Unbounded `last_n_numbers` allocation:** [4](#0-3) 

Since `last_n_blocks = 2^63` exceeds any realistic chain span, `last_block_number - start_block_number <= last_n_blocks` is always true, and `last_n_numbers` collects the entire range `(start_block_number..last_block_number)` — also uncapped.

**`complete_headers` amplifies the damage:** [5](#0-4) 

All three Vecs are chained and passed to `complete_headers`, which for every entry performs `snapshot.get_ancestor`, `snapshot.get_block`, `calc_uncles_hash`, and `snapshot.chain_root_mmr(...).get_root()`: [6](#0-5) 

The only meaningful pre-check is that `last_hash` must be on the main chain (line 210), which is publicly observable.

## Impact Explanation

With CKB mainnet chain height H ≈ 3.4M and `start_block_number = H/2 ≈ 1.7M`:
- `reorg_last_n_numbers`: ~1.7M entries
- `last_n_numbers`: ~1.7M entries
- `complete_headers` performs ~3.4M DB lookups + MMR root computations

Memory for the collected `VerifiableHeader` structs alone (each ~200–300 bytes) reaches ~680MB–1GB, causing OOM. This matches **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

- Any peer connecting via the light-client P2P protocol can send `GetLastStateProof` with no authentication
- The valid tip hash required for `last_hash` is publicly observable on-chain
- The crafted `start_hash` only needs to differ from the real ancestor — any random 32-byte value works
- The overflow is deterministic in release builds (standard CKB deployment uses `--release`)
- A single message is sufficient; no repetition or sustained attack is needed

## Recommendation

1. **Fix the overflow**: Replace the unchecked multiplication with a saturating operation:
   ```rust
   if self.message.difficulties().len()
       + (last_n_blocks as usize).saturating_mul(2)
       > constant::GET_LAST_STATE_PROOF_LIMIT
   ```

2. **Cap `reorg_last_n_numbers` independently**: After computing `min_block_number`, clamp the range length to at most `GET_LAST_STATE_PROOF_LIMIT` entries before collecting.

3. **Cap `last_n_numbers` independently**: The `(start_block_number..last_block_number)` range must also be bounded by the limit constant, not just by `last_n_blocks`.

4. **Add a combined post-collection length check**: Assert `reorg_last_n_numbers.len() + last_n_numbers.len() <= GET_LAST_STATE_PROOF_LIMIT` before calling `complete_headers`.

## Proof of Concept

```rust
// Craft the malicious GetLastStateProof message:
let msg = GetLastStateProof {
    last_hash:           /* valid current tip hash, publicly observable */,
    last_n_blocks:       1u64 << 63,       // 2^63 — overflows limit check to 0
    start_number:        1_700_000u64,     // ~H/2, any value ≤ chain height
    start_hash:          [0u8; 32].into(), // wrong hash → triggers reorg branch
    difficulty_boundary: U256::MAX,
    difficulties:        vec![],           // empty → difficulties.len() = 0
};
// Guard: 0 + (2^63_usize * 2) = 0 > 1000 → false → passes
// reorg_last_n_numbers = (0..1_700_000).collect()     → 1.7M entries
// last_n_numbers = (1_700_000..3_400_000).collect()   → 1.7M entries
// complete_headers called for ~3.4M entries → OOM → node crash
```

Send this single message to any CKB full node running the light-client protocol. No authentication, no prior state, and no repetition required.

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
