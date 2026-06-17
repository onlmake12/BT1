### Title
Unhandled Panic on Malformed Input in `update_price_feeds` Bypasses `#[handle_result]` Error Handling — (File: `target_chains/near/receiver/src/lib.rs`)

---

### Summary

The NEAR receiver contract's public `update_price_feeds` function uses `.unwrap()` directly on user-supplied byte data. When a caller submits input that is fewer than 4 bytes, or that begins with the accumulator magic header but is otherwise malformed, the function panics instead of returning a proper `Error`. This bypasses the `#[handle_result]` mechanism that the function is annotated with, and also prevents the promise chain (including the `refund_vaa` callback) from ever being constructed.

---

### Finding Description

`update_price_feeds` is a `#[payable]` `#[handle_result]` public function callable by any NEAR account. It accepts a hex-encoded price update payload as a `String`. [1](#0-0) 

At line 171, `cursor.clone().read_exact(&mut header).unwrap()` panics if the decoded byte slice is shorter than 4 bytes — the `read_exact` call returns an `Err` that is immediately unwrapped. [2](#0-1) 

At line 176, `AccumulatorUpdateData::try_from_slice(...).unwrap()` panics if the payload begins with the correct magic bytes (`PNAU`) but the remainder of the buffer is malformed and cannot be deserialized. [3](#0-2) 

The same pattern recurs in `verify_wormhole_batch_callback` (lines 248–263), which is invoked after Wormhole verification succeeds and processes the original user-supplied data: [4](#0-3) 

The `#[handle_result]` annotation on `update_price_feeds` is designed to propagate `Err` values gracefully. A Rust panic bypasses this entirely — the NEAR runtime catches the panic and aborts the transaction, but the `refund_vaa` promise callback is never registered, so the refund path depends entirely on the NEAR runtime's own deposit-revert behavior rather than the contract's explicit refund logic.

---

### Impact Explanation

In NEAR, a contract panic causes the transaction to abort and all state changes (including the attached deposit transfer) to be reverted by the runtime. The user's attached deposit is returned. However:

- The `refund_vaa` callback — the contract's own explicit refund mechanism — is never scheduled, because the panic occurs before the promise chain is constructed.
- The transaction fails with an opaque panic message rather than a typed `Error`, making it impossible for callers to distinguish this failure from other error conditions.
- Any user can trigger this with a trivially crafted payload (e.g., a hex string decoding to fewer than 4 bytes, or a string starting with `PNAU` followed by garbage bytes).

The impact is a forced transaction abort on any malformed accumulator-style input, with the contract's own refund logic bypassed. Funds are ultimately returned by the runtime, so direct fund loss does not occur. The severity is **Low**.

---

### Likelihood Explanation

The entry point is fully public and payable. No authentication or privileged role is required. Any account can call `update_price_feeds` with arbitrary `data`. The two panic conditions are trivially reachable:

1. Pass a hex string shorter than 8 characters (decodes to fewer than 4 bytes) → line 171 panics.
2. Pass a hex string whose first 4 decoded bytes equal `PNAU` but whose remainder is not a valid `AccumulatorUpdateData` → line 176 panics.

---

### Recommendation

Replace both `.unwrap()` calls with proper error propagation using `map_err` and `?`, consistent with the rest of the function's error handling:

```rust
// Line 171 — replace:
cursor.clone().read_exact(&mut header).unwrap();
// with:
cursor.clone().read_exact(&mut header).map_err(|_| Error::InvalidUpdateData)?;

// Line 176 — replace:
AccumulatorUpdateData::try_from_slice(&cursor.clone().into_inner()).unwrap();
// with:
AccumulatorUpdateData::try_from_slice(&cursor.clone().into_inner())
    .map_err(|_| Error::InvalidUpdateData)?;
```

Apply the same fix to the `.unwrap()` calls in `verify_wormhole_batch_callback` at lines 248, 249, 254, and 263. [4](#0-3) 

---

### Proof of Concept

```python
import requests, json

# Trigger line 171 panic: hex decodes to 3 bytes (< 4)
payload = {"data": "aabbcc"}
r = requests.post("https://<near-rpc>/", json={
    "jsonrpc": "2.0", "id": 1, "method": "broadcast_tx_commit",
    "params": [build_near_tx("update_price_feeds", payload)]
})
# Transaction aborts with panic: "called `Result::unwrap()` on an `Err` value"

# Trigger line 176 panic: starts with PNAU magic but rest is garbage
import binascii
magic = b"PNAU" + b"\xff" * 100
payload = {"data": binascii.hexlify(magic).decode()}
# Transaction aborts with panic in AccumulatorUpdateData::try_from_slice
```

### Citations

**File:** target_chains/near/receiver/src/lib.rs (L164-177)
```rust
    #[payable]
    #[handle_result]
    pub fn update_price_feeds(&mut self, data: String) -> Result<(), Error> {
        // Attempt to deserialize the Payload based on header.
        let bytes = &*hex::decode(data.clone()).map_err(|_| Error::InvalidHex)?;
        let cursor = &mut Cursor::new(bytes);
        let mut header = [0u8; 4];
        cursor.clone().read_exact(&mut header).unwrap();

        // Handle Accumulator style Price Updates.
        if &header == PYTHNET_ACCUMULATOR_UPDATE_MAGIC {
            let update_data =
                AccumulatorUpdateData::try_from_slice(&cursor.clone().into_inner()).unwrap();

```

**File:** target_chains/near/receiver/src/lib.rs (L247-264)
```rust
        // deserialize into an Action.
        let vaa = hex::decode(&vaa).unwrap();
        let vaa: wormhole_sdk::Vaa<&RawMessage> = serde_wormhole::from_slice(&vaa).unwrap();

        // Attempt to deserialize the Payload based on header.
        let bytes = &mut Cursor::new(vaa.payload);
        let mut header = [0u8; 4];
        bytes.clone().read_exact(&mut header).unwrap();

        // Check the header is a P2W header and return if not.
        if &header != P2W_MAGIC {
            return Err(Error::InvalidVaa);
        }

        // Verify the PriceAttestation's are new enough, and if so, store them.
        let mut count_updates = 0;
        let batch = BatchPriceAttestation::deserialize(bytes).unwrap();
        for price_attestation in &batch.price_attestations {
```
