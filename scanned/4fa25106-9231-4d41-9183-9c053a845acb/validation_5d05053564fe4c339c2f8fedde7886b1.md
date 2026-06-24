All three code references check out exactly as claimed. Let me confirm the key facts:

1. `util/types/src/conversion/storage.rs` L147-148: `txs_sizes: None` hardcoded for legacy 5-field `BlockExt` records. [1](#0-0) 

2. `store/src/store.rs` L252-253: `count_extra_fields() == 0` routes to the old reader (returning `txs_sizes: None`). [2](#0-1) 

3. `rpc/src/util/fee_rate.rs` L93: Unconditional `.expect(...)` on `txs_sizes` with no `None` guard. [3](#0-2) 

---

Audit Report

## Title
Unconditional `.expect()` on `txs_sizes` in `FeeRateCollector::statistics` Panics on Legacy 5-Field `BlockExt` Records - (File: `rpc/src/util/fee_rate.rs`)

## Summary
When a CKB node upgraded from pre-v0.106 retains legacy 5-field `BlockExt` database records within the last 1–101 blocks, calling the `get_fee_rate_statistics` RPC causes an unconditional `.expect("expect txs_size's length >= 1")` on a `None` value, panicking the node process. The root cause is that the legacy deserialization path hardcodes `txs_sizes: None`, while the RPC handler assumes `txs_sizes` is always `Some`.

## Finding Description
`BlockExt` was extended in v0.106 from a 5-field molecule table to a 7-field `BlockExtV1` table adding `cycles` and `txs_sizes`. In `util/types/src/conversion/storage.rs` (L147-148), the `Unpack<core::BlockExt> for packed::BlockExtReader` implementation unconditionally sets `txs_sizes: None` for all legacy records. In `store/src/store.rs` (L252-253), `get_block_ext` dispatches on `count_extra_fields()`: 0 extra fields → old reader (yields `txs_sizes: None`), 2 extra fields → `BlockExtV1Reader` (properly unpacks both fields). In `rpc/src/util/fee_rate.rs` (L93), `FeeRateCollector::statistics` calls `txs_sizes.expect("expect txs_size's length >= 1")` with no guard for `None`. When any block in the collection window (up to 101 blocks back from tip) was stored in the old format, this `expect` panics. No existing check in the RPC handler, the `collect` iterator, or the store layer prevents `None` from reaching the `expect` call.

## Impact Explanation
A panic in the RPC handler crashes the node process (Rust panics are fatal unless explicitly caught). This constitutes a denial of service against the node, matching **Note (0–500 points): Any local RPC API crash**. The preconditions (legacy blocks within the last 101 blocks) limit this to nodes recently upgraded from pre-v0.106, making it unlikely to affect mainnet nodes at scale but concretely exploitable on testnets, private networks, or nodes that were offline for a long time before upgrading.

## Likelihood Explanation
Any caller with access to the RPC port (unauthenticated if the port is exposed) can trigger the panic with a single JSON-RPC call. The precondition — legacy 5-field `BlockExt` records within the last 101 blocks — is realistic for nodes upgraded from pre-v0.106 on testnets or private networks, or nodes that synced from a snapshot containing legacy records near the tip. On mainnet, the window of 101 blocks is extremely unlikely to reach pre-v0.106 blocks given the chain length, but the code path is permanently broken for any node in the described state.

## Recommendation
Replace the unconditional `.expect(...)` on line 93 of `rpc/src/util/fee_rate.rs` with a guard that skips blocks where `txs_sizes` is `None`:
```rust
let Some(txs_sizes) = txs_sizes else { return fee_rates; };
```
This makes the RPC gracefully skip legacy blocks rather than panicking, consistent with how `cycles` is already handled with `if let Some(cycles) = cycles` on line 97.

## Proof of Concept
1. Start a CKB node that was upgraded from pre-v0.106, with fewer than 101 blocks mined after the upgrade (or use a private/test network with legacy blocks near the tip).
2. Send the following RPC call:
   ```json
   {"id": 1, "jsonrpc": "2.0", "method": "get_fee_rate_statistics", "params": []}
   ```
3. The node panics at `rpc/src/util/fee_rate.rs:93` with message `expect txs_size's length >= 1` because `FeeRateCollector::statistics` encounters a `BlockExt` with `txs_sizes: None` deserialized from a legacy 5-field database record, crashing the node process.

### Citations

**File:** util/types/src/conversion/storage.rs (L139-151)
```rust
impl<'r> Unpack<core::BlockExt> for packed::BlockExtReader<'r> {
    fn unpack(&self) -> core::BlockExt {
        core::BlockExt {
            received_at: self.received_at().into(),
            total_difficulty: self.total_difficulty().into(),
            total_uncles_count: self.total_uncles_count().into(),
            verified: self.verified().into(),
            txs_fees: self.txs_fees().into(),
            cycles: None,
            txs_sizes: None,
        }
    }
}
```

**File:** store/src/store.rs (L247-263)
```rust
    fn get_block_ext(&self, block_hash: &packed::Byte32) -> Option<BlockExt> {
        self.get(COLUMN_BLOCK_EXT, block_hash.as_slice())
            .map(|slice| {
                let reader =
                    packed::BlockExtReader::from_compatible_slice_should_be_ok(slice.as_ref());
                match reader.count_extra_fields() {
                    0 => reader.into(),
                    2 => packed::BlockExtV1Reader::from_slice_should_be_ok(slice.as_ref()).into(),
                    _ => {
                        panic!(
                            "BlockExt storage field count doesn't match, expect 7 or 5, actual {}",
                            reader.field_count()
                        )
                    }
                }
            })
    }
```

**File:** rpc/src/util/fee_rate.rs (L86-93)
```rust
        let mut fee_rates = self.provider.collect(target, |mut fee_rates, block_ext| {
            let BlockExt {
                txs_sizes,
                cycles,
                txs_fees,
                ..
            } = block_ext;
            let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
```
