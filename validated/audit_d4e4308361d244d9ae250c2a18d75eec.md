### Title
Network Alert Signature Replay Across CKB Networks Due to Missing Chain Domain Separator - (`util/network-alert/src/verifier.rs`)

---

### Summary

The CKB network alert system signs and verifies alerts using only the raw `RawAlert` content hash, with no chain identifier, genesis hash, or network domain separator included in the signed message. Because the same hardcoded public keys are used across all CKB networks (mainnet, testnet, devnet), a valid alert signature produced for one network is cryptographically valid on every other CKB network. An unprivileged P2P peer or RPC caller can replay a legitimately signed testnet alert on mainnet nodes (or vice versa), causing all receiving nodes to accept and propagate a false alert.

---

### Finding Description

The `verify_signatures()` function in `util/network-alert/src/verifier.rs` computes the message to verify as:

```rust
let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
```

`calc_alert_hash()` is implemented in `util/gen-types/src/extension/calc_hash.rs` as:

```rust
pub fn calc_alert_hash(&self) -> packed::Byte32 {
    self.calc_hash()   // = blake2b_256(self.as_slice())
}
```

This is a plain `blake2b_256` over the raw molecule-encoded `RawAlert` bytes. The `RawAlert` schema (defined in `util/gen-types/schemas/extensions.mol`) contains only:

```
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

None of these fields encode a chain identifier, genesis block hash, or any other network-specific value. The signed digest is therefore identical for the same `RawAlert` content regardless of which CKB network the alert is intended for.

The hardcoded alert public keys are embedded in `util/app-config/src/configs/alert_signature.toml` and loaded as the default for every network:

```toml
signatures_threshold = 2
public_keys = [
  "0x03933a9b116c5017561742c37ae69acb0dca3329a52c479c85df5bb387c8ac8715",
  ...
]
```

Because the same key set is the default for mainnet, testnet, and devnet, a signature that is valid on one network is valid on all others.

---

### Impact Explanation

An attacker who observes a legitimately signed alert on the CKB testnet (alerts are public P2P messages) can relay the exact same `packed::Alert` bytes to mainnet nodes. Every mainnet node will:

1. Parse the alert successfully (same molecule schema).
2. Call `verify_signatures()`, which recomputes `blake2b_256(raw_alert_bytes)` — identical to the testnet hash — and confirms the Nervos Foundation signatures are valid.
3. Accept the alert, store it, display it to the operator, and re-broadcast it to all connected peers.

The result is network-wide propagation of a false or misleading alert on mainnet (e.g., a testnet-only upgrade notice displayed to all mainnet operators), or the reverse. This can cause operator panic, unnecessary node downgrading/upgrading, or be used to mask a real alert by pre-occupying an alert ID slot via replay.

---

### Likelihood Explanation

All signed alerts are broadcast over the public P2P network and are also injectable via the `send_alert` RPC endpoint (when the Alert RPC module is enabled). Any unprivileged peer connected to the testnet can observe a valid signed alert and immediately replay it against mainnet peers. No key material is required; the attacker only needs to capture the already-signed `packed::Alert` bytes. The Nervos Foundation has historically issued real alerts (e.g., alert `20230001`), so valid signed material exists in the wild.

---

### Recommendation

Include a network-specific domain separator in the signed digest. The most natural binding is the genesis block hash, which is unique per CKB network and already available in the node. Replace the bare `calc_alert_hash()` with a hash that commits to both the `RawAlert` content and the genesis hash:

```rust
// In verify_signatures():
let mut hasher = new_blake2b();
hasher.update(genesis_hash.as_slice());          // chain-specific binding
hasher.update(alert.raw().as_slice());
let mut message_bytes = [0u8; 32];
hasher.finalize(&mut message_bytes);
let message = Message::from_slice(&message_bytes)?;
```

The `Verifier` struct should be constructed with the genesis hash so it can include it in every verification. Alert signers must adopt the same construction when producing signatures.

---

### Proof of Concept

1. Run a CKB testnet node and a CKB mainnet node (both with default alert keys).
2. On testnet, submit a valid signed alert via `send_alert` RPC (using the real Nervos Foundation signatures from alert `20230001`, or any future testnet alert).
3. Capture the raw `packed::Alert` bytes as they propagate over the P2P Alert protocol.
4. Connect to a mainnet peer and send the identical bytes over the Alert protocol sub-stream.
5. Observe that the mainnet node's `verify_signatures()` accepts the alert (same keys, same hash), stores it, and re-broadcasts it to all its mainnet peers.

**Root cause trace:**

- Entry: P2P `Alert` protocol message received → `alert_relayer.rs:received()` [1](#0-0) 
- Verification: `verifier.verify_signatures(&alert)` uses only `alert.calc_alert_hash()` with no chain binding [2](#0-1) 
- Hash computation: `calc_alert_hash()` = `blake2b_256(raw_alert_bytes)` only [3](#0-2) 
- Schema: `RawAlert` contains no network identifier field [4](#0-3) 
- Same keys for all networks: hardcoded default in `alert_signature.toml` [5](#0-4)

### Citations

**File:** util/network-alert/src/alert_relayer.rs (L151-162)
```rust
        if let Err(err) = self.verifier.verify_signatures(&alert) {
            debug!(
                "An alert from peer {} with invalid signatures, error {:?}",
                peer_index, err
            );
            nc.ban_peer(
                peer_index,
                BAD_MESSAGE_BAN_TIME,
                String::from("send us an alert with invalid signatures"),
            );
            return;
        }
```

**File:** util/network-alert/src/verifier.rs (L33-35)
```rust
    pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
        trace!("Verifying alert {:?}", alert);
        let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
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

**File:** util/app-config/src/configs/alert_signature.toml (L1-8)
```text
# need 2 signatures to send alert
signatures_threshold = 2
public_keys = [
  "0x03933a9b116c5017561742c37ae69acb0dca3329a52c479c85df5bb387c8ac8715",
  "0x038da23240e5a4234601902cf3db3cdfc1b3fdb2db2a54ba6204f6ca1d6ef6129a",
  "0x02adc94b64a9809019139fe70bd26aa0d787772a1ad645a4bcb1456fb3e1105f09",
  "0x0369eca725513fc94685cd0b8ccebc7be874afea38d23ccb090566aa1c50d696b1",
]
```
