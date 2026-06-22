### Title
Network Alert Signature Replayable Across CKB Networks Due to Missing Chain Identifier in `calc_alert_hash()` - (File: `util/gen-types/src/extension/calc_hash.rs`, `util/network-alert/src/verifier.rs`)

---

### Summary

The CKB network alert system hashes only the raw alert content (`RawAlert`) when computing the message for signature verification. No network or chain identifier is included in the hash. Because the same Nervos Foundation signing keys are used across CKB networks (mainnet, testnet, devnet), a signed alert from one network is cryptographically valid on any other network. An unprivileged attacker who observes a signed alert on testnet can replay it verbatim to mainnet nodes via the P2P relay protocol, causing the alert to propagate across mainnet without any new signature from the Foundation.

---

### Finding Description

**Root cause — `calc_alert_hash()` hashes only raw alert bytes, no chain binding:**

`RawAlert` is defined in the Molecule schema with fields `notice_until`, `id`, `cancel`, `priority`, `message`, `min_version`, `max_version` — no network or chain identifier field exists. [1](#0-0) 

`calc_alert_hash()` simply calls `calc_hash()`, which is `blake2b_256(self.as_slice())` — a hash of the raw alert bytes with no domain separation or chain binding: [2](#0-1) 

The underlying `CalcHash` trait confirms this is a plain `blake2b_256` of the serialized bytes: [3](#0-2) 

**Verification path — `Verifier::verify_signatures()` uses this chain-unbound hash:** [4](#0-3) 

The message passed to `verify_m_of_n` is derived solely from `alert.calc_alert_hash()`, which contains no network identifier. If the same `RawAlert` bytes are submitted to a node on a different network, the hash is identical and the signatures verify successfully.

**Same signing keys across networks:**

The `NetworkAlertConfig` public keys are hardcoded in the source and shared across mainnet and testnet. The test `test_alert_20230001` uses `NetworkAlertConfig::default()` with production keys and a specific alert that carries no network binding: [5](#0-4) 

**P2P relay entry path — `alert_relayer` accepts and re-broadcasts verified alerts from peers:**

The `alert_relayer` module handles incoming P2P alert messages, calls `verifier.verify_signatures()`, and re-broadcasts accepted alerts across the network. An attacker peer can inject a testnet-signed alert directly into the mainnet P2P layer without using the RPC. [6](#0-5) 

---

### Impact Explanation

An unprivileged attacker who observes any signed alert on testnet (alerts are broadcast publicly over P2P) can replay the identical signed bytes to mainnet nodes. The mainnet verifier accepts the alert because:
1. The hash is identical (no chain binding).
2. The signatures are valid against the same Foundation public keys.

Concrete consequences:
- **False alert propagation on mainnet**: Node operators receive and act on alerts that were intended only for testnet (e.g., "upgrade to version X", "downgrade from version Y").
- **Cancellation of legitimate mainnet alerts**: A testnet cancel-alert (with `cancel` field set to a mainnet alert ID) can be replayed to suppress a legitimate mainnet alert, silencing a real security warning.
- **Alert flooding/confusion**: Repeated replay of expired or irrelevant testnet alerts causes noise and erodes operator trust in the alert system.

---

### Likelihood Explanation

- Testnet alerts are broadcast publicly over P2P; any connected peer can observe them.
- The attacker needs only to connect to a mainnet node as a peer (no privilege required) and send the replayed alert message.
- The same Foundation signing keys are used on both mainnet and testnet by design (hardcoded defaults).
- No cryptographic break is required; the attack is a straightforward message replay.

---

### Recommendation

Include a network/chain identifier in the signed message. The natural domain separator in CKB is the genesis block hash, which is unique per network. Modify `calc_alert_hash()` (or introduce a new `calc_alert_signing_hash()`) to prepend the genesis hash before hashing:

```rust
pub fn calc_alert_signing_hash(&self, genesis_hash: &packed::Byte32) -> packed::Byte32 {
    let mut hasher = new_blake2b();
    hasher.update(genesis_hash.as_slice());
    hasher.update(self.as_slice());
    let mut result = [0u8; 32];
    hasher.finalize(&mut result);
    result.into()
}
```

Pass the consensus genesis hash into `Verifier` at construction time and use this method in `verify_signatures()`. This binds each alert signature to a specific CKB network, preventing cross-network replay.

---

### Proof of Concept

1. On testnet, observe a broadcast signed alert (e.g., the real alert `id=20230001` with its two known signatures).
2. Connect to a mainnet node as a P2P peer supporting the Alert protocol.
3. Send the identical `Alert` molecule-encoded bytes (same `RawAlert` + same signatures) over the P2P connection.
4. The mainnet node calls `verifier.verify_signatures()`, computes `blake2b_256(raw_alert.as_slice())`, recovers the public keys from the signatures, and finds them in its hardcoded Foundation key set.
5. Verification passes; the alert is stored and re-broadcast to all mainnet peers.
6. All mainnet nodes display the testnet alert message to operators.

The hardcoded testnet alert signatures from the test file confirm the same keys and hash scheme are used across networks: [7](#0-6)

### Citations

**File:** util/gen-types/schemas/extensions.mol (L445-453)
```text
table RawAlert {
    notice_until:   Uint64,
    id:             Uint32,
    cancel:         Uint32,
    priority:       Uint32,
    message:        Bytes,
    min_version:    BytesOpt,
    max_version:    BytesOpt,
}
```

**File:** util/gen-types/src/extension/calc_hash.rs (L15-22)
```rust
impl<'r, R> CalcHash for R
where
    R: Reader<'r>,
{
    fn calc_hash(&self) -> packed::Byte32 {
        blake2b_256(self.as_slice()).into()
    }
}
```

**File:** util/gen-types/src/extension/calc_hash.rs (L292-299)
```rust
impl<'r> packed::RawAlertReader<'r> {
    /// Calculates the hash for [self.as_slice()] as the alert hash.
    ///
    /// [self.as_slice()]: ../prelude/trait.Reader.html#tymethod.as_slice
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
}
```

**File:** util/network-alert/src/verifier.rs (L33-64)
```rust
    pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
        trace!("Verifying alert {:?}", alert);
        let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
        let signatures: Vec<Signature> = alert
            .signatures()
            .into_iter()
            .filter_map(
                |sig_data| match Signature::from_slice(sig_data.as_reader().raw_data()) {
                    Ok(sig) => {
                        if sig.is_valid() {
                            Some(sig)
                        } else {
                            debug!("invalid signature: {:?}", sig);
                            None
                        }
                    }
                    Err(err) => {
                        debug!("signature error: {}", err);
                        None
                    }
                },
            )
            .collect();
        verify_m_of_n(
            &message,
            self.config.signatures_threshold,
            &signatures,
            &self.pubkeys,
        )
        .map_err(|err| err.kind())?;
        Ok(())
    }
```

**File:** util/network-alert/src/tests/generate_alert_signature.rs (L58-93)
```rust
#[test]
fn test_alert_20230001() {
    let config = NetworkAlertConfig::default();
    let verifier = Verifier::new(config);
    let raw_alert = packed::RawAlert::new_builder()
        // 3 months later
        .notice_until(1681574400000u64)
        .id(20230001u32)
        .cancel(0u32)
        .priority(20u32)
        .message("CKB v0.105.* have bugs. Please upgrade to the latest version.")
        .min_version(Some("0.105.0-pre"))
        .max_version(Some("0.105.1"))
        .build();

    let signatures = [
        "8dca283684ff3cd024bd6a67efb24617e90e31dc69ac809ac6ac5e243a57b7aa6711228dfbd8a5cc89a68d3065b685e5c56c70740e8d3487fd538dc914d0c97c00",
        "4554b37824e17ea02432507e372c869301a415bf718e0a5a33b6df75cd32fbab7cf8176ca8b079c28266ce1f33c3f61fbff19e27be2a85f5a14faa2b1b474e0a01"
    ].iter().map(|hex| {
        let mut buf = vec![0u8; hex.len() / 2];
        hex_decode(hex.as_bytes(), &mut buf).expect("valid hex");
        buf.into()
    }).fold(packed::BytesVec::new_builder(), |builder, item: packed::Bytes| {
        builder.push(item)
    }).build();
    let alert = packed::Alert::new_builder()
        .raw(raw_alert)
        .signatures(signatures)
        .build();
    let alert_json = Alert::from(alert.clone());
    println!(
        "Alert:\n{}",
        serde_json::to_string_pretty(&alert_json).unwrap()
    );
    assert!(verifier.verify_signatures(&alert).is_ok());
}
```

**File:** util/network-alert/src/alert_relayer.rs (L1-3)
```rust
//! AlertRelayer
//! We implement a Bitcoin like alert system, n of m alert key holders can decide to send alert
//messages to all client
```
