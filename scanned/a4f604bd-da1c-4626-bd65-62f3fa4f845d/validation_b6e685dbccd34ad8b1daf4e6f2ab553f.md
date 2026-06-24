Audit Report

## Title
Unconditional `.expect()` on `txs_sizes: None` in `FeeRateCollector::statistics` Panics on Legacy `BlockExt` Records - (File: `rpc/src/util/fee_rate.rs`)

## Summary
When a CKB node upgraded from pre-v0.106 retains 5-field `BlockExt` records in its database, the `get_fee_rate_statistics` RPC handler panics unconditionally because `FeeRateCollector::statistics` calls `.expect(...)` on `txs_sizes` without guarding against `None`. The deserialization path for legacy 5-field records hardcodes `txs_sizes: None`, making the panic deterministic for any node whose 101-block statistics window includes pre-v0.106 blocks. The panic aborts the RPC task but does not crash the node process, as the server runs on Tokio with no `panic = "abort"` profile set.

## Finding Description
`BlockExt` was extended in v0.106 from a 5-field molecule table to a 7-field `BlockExtV1` table. The legacy deserialization path unconditionally sets `txs_sizes: None`:

`util/types/src/conversion/storage.rs` lines 139–151 — `Unpack<core::BlockExt> for packed::BlockExtReader` hardcodes both new fields to `None`.

`store/src/store.rs` lines 247–263 — `get_block_ext` dispatches on `count_extra_fields()`: a value of `0` routes to the old reader (returning `txs_sizes: None`), while `2` routes to `BlockExtV1Reader` (which properly unpacks both fields).

`rpc/src/util/fee_rate.rs` line 93 — `FeeRateCollector::statistics` calls `txs_sizes.expect("expect txs_size's length >= 1")` with no `None` guard. Any block in the 1–101 block window that was stored in the old format triggers the panic.

The RPC entry points at `rpc/src/module/chain.rs` lines 2124–2132 (`get_fee_rate_statics` and `get_fee_rate_statistics`) both call `FeeRateCollector::new(...).statistics(...)` directly, with no intervening error handling.

The RPC server (`rpc/src/server.rs` line 133) is spawned as a Tokio task via `handler.spawn`. No `panic = "abort"` profile is present in the workspace. Tokio catches panics at async task boundaries, so the panic aborts the connection/task but does not terminate the node process.

## Impact Explanation
The impact is **Note (0–500 points): Any local RPC API crash**. The panic terminates the Tokio task handling the RPC connection, dropping the client connection, but the node process itself continues running. The claimed "High" severity (full node crash / denial of service) is not supported: without `panic = "abort"`, Tokio's task infrastructure catches the unwind and the node remains operational. The impact is limited to the RPC call failing for any caller who triggers the affected code path under the described preconditions.

## Likelihood Explanation
Requires a node that was upgraded from a version prior to v0.106 and has fewer than 101 blocks mined after the upgrade, so that the statistics window includes legacy 5-field `BlockExt` records. On mainnet this is practically impossible (v0.106 is many thousands of blocks old). On testnets, private networks, or nodes that were offline for a long time and then upgraded, the condition is realistic. Any RPC caller (including unauthenticated ones if the RPC port is exposed) can trigger the panic with a single call.

## Recommendation
Replace the unconditional `.expect(...)` at `rpc/src/util/fee_rate.rs` line 93 with a guard that skips blocks where `txs_sizes` is `None`:
```rust
let txs_sizes = match txs_sizes {
    Some(sizes) if !sizes.is_empty() => sizes,
    _ => return fee_rates, // skip legacy blocks lacking txs_sizes
};
```
This makes the statistics collection gracefully skip pre-v0.106 blocks rather than panicking.

## Proof of Concept
1. Run a CKB node upgraded from pre-v0.106 on a private or test network with fewer than 101 blocks mined after the upgrade.
2. Send:
   ```json
   {"id": 1, "jsonrpc": "2.0", "method": "get_fee_rate_statistics", "params": []}
   ```
3. The Tokio task handling the request panics at `rpc/src/util/fee_rate.rs:93` with `expect("expect txs_size's length >= 1")`. The connection is dropped; the node process continues. Repeatable on every call while legacy blocks remain in the statistics window.