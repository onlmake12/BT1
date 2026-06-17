The code is clear enough to make a determination. Here is the analysis:

**`price_status::from_u64` (line 16):**
```move
public fun from_u64(status: u64): PriceStatus {
    assert!(status <= TRADING, 0);  // TRADING = 1
    PriceStatus { status }
}
```
Aborts with error code 0 for any status byte > 1.

**`deserialize_price_info` (line 123):**
```move
let status = price_status::from_u64((deserialize::deserialize_u8(cur) as u64));
```
The raw status byte from the wire payload is passed directly to `from_u64` with no prior bounds check.

**`create_price_feeds` (line 152):**
```move
let price_infos = batch_price_attestation::destroy(batch_price_attestation::deserialize(vaa::take_payload(vaa), clock));
```
Called after the emitter/chain check passes. No guard exists between VAA verification and deserialization.

The Pyth v2 batch attestation wire format defines four status values: UNKNOWN=0, TRADING=1, HALTED=2, AUCTION=3. The Sui contract only accepts 0 and 1. Any legitimate VAA from Pythnet carrying a HALTED or AUCTION attestation will cause the entire transaction to abort. An unprivileged relayer cannot forge guardian signatures, but they can selectively relay any existing guardian-signed VAA from an approved emitter that contains a status=2 or status=3 byte. The abort propagates atomically — all price feeds in the batch are blocked.

---

### Title
`price_status::from_u64` Rejects Valid Wire-Format Status Values, Causing Batch Deserialization Abort — (`target_chains/sui/contracts/sources/price_status.move`)

### Summary
`price_status::from_u64` unconditionally aborts on any status byte greater than 1. The Pyth v2 batch attestation wire format defines HALTED (2) and AUCTION (3) as valid status values. A relayer submitting a guardian-signed VAA from an approved data source whose payload contains any attestation with status ≥ 2 will cause the entire `create_price_feeds` (or `create_price_infos_hot_potato`) transaction to abort, blocking all price feeds in the batch.

### Finding Description
`price_status::from_u64` enforces `status <= 1`: [1](#0-0) 

`deserialize_price_info` reads the status byte from the raw cursor and passes it directly to `from_u64` without any prior clamping or range check: [2](#0-1) 

`create_price_feeds` calls `batch_price_attestation::deserialize` immediately after the emitter/chain check, with no intervening guard on the status byte: [3](#0-2) 

The same abort path is reachable through `create_price_infos_hot_potato`: [4](#0-3) 

The Pyth v2 batch attestation specification defines four status values (UNKNOWN=0, TRADING=1, HALTED=2, AUCTION=3). The Sui contract silently discards the HALTED/AUCTION distinction in its own logic (lines 141–151 of `batch_price_attestation.move` fall back to `prev_price` for any non-TRADING status), but the `from_u64` gate fires before that fallback is ever reached. [5](#0-4) 

### Impact Explanation
Any transaction calling `create_price_feeds` or `create_price_infos_hot_potato` with a VAA whose payload contains at least one attestation with status byte ≥ 2 aborts atomically. All price feeds in the batch are blocked from being created or updated for as long as the attacker continues to relay such VAAs. Because Sui transactions are atomic, a single offending attestation in a multi-attestation batch poisons the entire batch.

### Likelihood Explanation
Pythnet legitimately publishes HALTED and AUCTION status values during market closures and auction phases. Any unprivileged relayer can observe such a VAA on-chain (Wormhole VAAs are public), verify it passes guardian signature and emitter checks, and submit it to the Sui contract. No key compromise, governance majority, or privileged access is required. The relayer only needs to call `wormhole::vaa::parse_and_verify` followed by `pyth::create_price_feeds`.

### Recommendation
Replace the hard abort in `from_u64` with a saturating clamp: any status value not equal to TRADING (1) should be treated as UNKNOWN (0). This matches the fallback logic already present in `deserialize_price_info` (lines 141–151) and aligns with how every other Pyth chain implementation handles unknown status values.

```move
public fun from_u64(status: u64): PriceStatus {
    // Treat any unrecognised status as UNKNOWN
    let normalised = if (status == TRADING) { TRADING } else { UNKNOWN };
    PriceStatus { status: normalised }
}
```

### Proof of Concept
1. Construct a minimal batch attestation byte vector with `attestation_count = 1`, valid magic `0x50325748`, and set the status byte (offset 32+32+8+8+4+8+8 = 100 bytes into the attestation body) to `0x02`.
2. Wrap the payload in a VAA signed by a test guardian key (the test suite already uses `x"beFA429d57cD18b7F8A4d91A2da9AB4AF05d0FBe"` as the guardian).
3. Call `vaa::parse_and_verify` then `pyth::create_price_feeds`.
4. Assert the transaction aborts with error code 0 (the abort code emitted by `assert!(status <= TRADING, 0)` in `from_u64`).

The existing test `test_invalid_price_status` already confirms the abort path for status=3: [6](#0-5) 

The same abort fires for status=2 (HALTED), which is a valid wire-format value.

### Citations

**File:** target_chains/sui/contracts/sources/price_status.move (L15-20)
```text
    public fun from_u64(status: u64): PriceStatus {
        assert!(status <= TRADING, 0);
        PriceStatus {
            status
        }
    }
```

**File:** target_chains/sui/contracts/sources/price_status.move (L48-52)
```text
    #[test]
    #[expected_failure]
    fun test_invalid_price_status() {
        from_u64(3);
    }
```

**File:** target_chains/sui/contracts/sources/batch_price_attestation.move (L123-123)
```text
        let status = price_status::from_u64((deserialize::deserialize_u8(cur) as u64));
```

**File:** target_chains/sui/contracts/sources/batch_price_attestation.move (L141-151)
```text
        if (status != price_status::new_trading()) {
            current_price = pyth::price::new(prev_price, prev_conf, expo, prev_publish_time);
        };

        // If status is trading, use the timestamp of the aggregate as the timestamp for the
        // EMA price. If not, the EMA will have last been updated when the aggregate last had
        // trading status, so use prev_publish_time (the time when the aggregate last had trading status).
        let ema_timestamp = publish_time;
        if (status != price_status::new_trading()) {
            ema_timestamp = prev_publish_time;
        };
```

**File:** target_chains/sui/contracts/sources/pyth.move (L151-152)
```text
            // Deserialize the batch price attestation
            let price_infos = batch_price_attestation::destroy(batch_price_attestation::deserialize(vaa::take_payload(vaa), clock));
```

**File:** target_chains/sui/contracts/sources/pyth.move (L243-243)
```text
            let price_infos = batch_price_attestation::destroy(batch_price_attestation::deserialize(vaa::take_payload(cur_vaa), clock));
```
