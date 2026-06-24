Audit Report

## Title
Integer overflow in `GetLastStateProofProcess::execute` bypasses `GET_LAST_STATE_PROOF_LIMIT`, enabling unbounded server-side work per light-client request — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The guard at line 201 intended to cap server work computes `(last_n_blocks as usize) * 2` without overflow protection. With `last_n_blocks = 2^63` (a valid wire value), this multiplication wraps to `0` in Rust release mode, bypassing the limit entirely. The server then collects and processes every block from `start_block_number` to the chain tip, performing multiple expensive DB and MMR operations per block with no effective upper bound.

## Finding Description
**Root cause — line 201:**
```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
```
`last_n_blocks` is a `u64` read directly from a molecule `Uint64` field, accepting all values 0..2^64-1. On a 64-bit target, `usize` is also 64 bits, so `last_n_blocks as usize` with value `2^63` is a no-op. The subsequent `* 2` computes `2^64`, which overflows `usize` and wraps to `0` in Rust release mode (defined wrapping behavior). With an empty `difficulties` list, the full expression is `0 + 0 > 1000` → `false` → guard skipped.

**Exploit path — line 291:**
```rust
let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
    <= last_n_blocks   // 2^63 — always true for any real chain height
{
    let sampled_numbers = Vec::new();
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
    (sampled_numbers, last_n_numbers)
```
Because `last_n_blocks = 2^63` is larger than any realistic chain height, the condition is always true and `last_n_numbers` collects every block number from `start_block_number` to the tip. `complete_headers` is then called for every entry (line 359), executing `get_ancestor`, `get_block`, `calc_uncles_hash`, and `chain_root_mmr(...).get_root()` per block — all expensive DB/MMR operations.

**No other guard intervenes:** the `start_block_number > last_block_number` check (line 231) and the `reorg_last_n_numbers` path (line 237) do not bound the size of `last_n_numbers`.

## Impact Explanation
For a chain of height H, a single malicious request causes O(H) expensive operations: full block fetches, uncle hash computations, and MMR root calculations. On a long-running mainnet node (height in the millions), this allocates gigabytes of `VerifiableHeader` data and saturates the light-client server thread, leading to OOM crash or sustained denial-of-service against the node. This matches **High: Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation
Any peer speaking the light-client protocol can send this message — no proof-of-work, key, or privilege is required. The molecule `Uint64` encoding accepts all 64-bit values, making the crafted message trivial to construct. The attack is repeatable and can be sent in a tight loop from a single connection.

## Recommendation
Replace the unchecked multiplication with a saturating variant before the comparison:
```rust
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```
Alternatively, add an explicit early rejection before any arithmetic:
```rust
if last_n_blocks > constant::GET_LAST_STATE_PROOF_LIMIT as u64 {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

## Proof of Concept
1. Connect to a CKB node as a light-client peer on a chain of height H > 1000.
2. Send `GetLastStateProof { last_hash: tip_hash, start_hash: genesis_hash, start_number: 0, last_n_blocks: 9223372036854775808 /* 2^63 */, difficulty_boundary: U256::MAX, difficulties: [] }`.
3. Observe: the guard at line 201 evaluates to `0 > 1000 = false` (overflow), the condition at line 291 evaluates to `true` (H ≤ 2^63), and `complete_headers` is invoked for all H block numbers.
4. **Unit test:** build a chain of height H > 1000 in the existing test harness, send the above message, and assert the server rejects it or processes no more than 1000 block numbers — currently it processes all H. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
