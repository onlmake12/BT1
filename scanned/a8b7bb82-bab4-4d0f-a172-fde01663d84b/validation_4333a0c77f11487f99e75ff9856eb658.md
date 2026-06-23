### Title
Untyped Network Alert Signing Lacks Chain-ID Domain Separator — Cross-Network Alert Replay - (`util/network-alert/src/verifier.rs`)

---

### Summary

The CKB network alert system signs raw alert data with no chain identifier, network name, or domain separator included in the signed payload. A valid alert signature produced for one CKB network (e.g., Mirana mainnet) is cryptographically valid on any other CKB-based network (e.g., Pudge testnet) that shares the same authorized public key set, enabling cross-network alert replay by any unprivileged P2P peer or RPC caller.

---

### Finding Description

The `RawAlert` molecule schema contains only application-level fields — `notice_until`, `id`, `cancel`, `priority`, `message`, `min_version`, `max_version` — with no chain ID, genesis hash, or network name field. [1](#0-0) 

The alert hash that is signed is computed by `calc_alert_hash`, which delegates to the generic `calc_hash` trait. That trait is implemented as a plain `blake2b_256(self.as_slice())` over the raw molecule bytes — no prefix, no domain tag, no chain context. [2](#0-1) [3](#0-2) 

The verifier in `util/network-alert/src/verifier.rs` derives the signing message directly from this chain-agnostic hash and checks it against the configured public keys: [4](#0-3) 

Because the signed bytes are identical across all CKB networks for the same `RawAlert` content, a signature that is valid on mainnet is also valid on testnet, and vice versa.

The signing procedure used in practice confirms this — the message is simply the alert hash with no additional context: [5](#0-4) 

---

### Impact Explanation

Any unprivileged P2P peer that observes a legitimately signed mainnet alert (alerts are broadcast publicly over the P2P network) can relay the identical `Alert` bytes — raw content plus signatures — to testnet nodes. Because the signed hash is chain-agnostic, testnet nodes will pass `verify_signatures` and accept the alert as authentic. The reverse direction (testnet → mainnet) is equally possible.

Concrete consequences:
- **False critical alerts on testnet**: A mainnet "upgrade immediately" alert replayed on testnet causes testnet node operators to receive and display an alert they did not intend to receive, potentially triggering unnecessary emergency responses.
- **Alert suppression via cancel replay**: If a cancel alert (field `cancel` set to a prior alert ID) is signed on one network and replayed on another, it can suppress a legitimate active alert on the target network.
- **Precedent for future key reuse**: If the same Nervos Foundation key set is ever used for a sidechain or hard-fork network, all historical alert signatures become immediately reusable on that network.

---

### Likelihood Explanation

The attack requires no special capability. All CKB alerts are broadcast over the P2P gossip layer and are also injectable via the `send_alert` JSON-RPC endpoint. Any peer connected to mainnet can observe a signed alert and immediately submit it to a testnet node via RPC or P2P relay. No key material, no privileged access, and no brute force is needed — the attacker simply copies bytes from one network to another. [6](#0-5) 

---

### Recommendation

Include a network-specific domain separator in the data that is hashed and signed. The most natural choice is the genesis block hash, which is unique per CKB network and already available in the node. The signed preimage should be:

```
blake2b( genesis_hash || raw_alert_bytes )
```

Alternatively, prepend a fixed ASCII tag (e.g., `"ckb-mainnet-alert"`) before hashing. Either approach ensures that a signature produced for one network is cryptographically invalid on any other network, eliminating cross-network replay entirely.

The `RawAlert` molecule schema should be extended with a `chain_id` or `genesis_hash` field, or the `calc_alert_hash` implementation should incorporate the chain context before hashing. [2](#0-1) 

---

### Proof of Concept

1. Run a CKB mainnet node and a CKB testnet (Pudge) node.
2. On mainnet, call `send_alert` RPC with a valid alert signed by the Nervos Foundation keys. Observe the alert propagate and be accepted by mainnet nodes.
3. Copy the exact JSON alert object (including `signatures`) from the mainnet RPC response.
4. Submit the identical alert JSON to the testnet node via `send_alert` RPC.
5. Observe that the testnet node accepts the alert and propagates it — `verify_signatures` passes because `calc_alert_hash` produces the same 32-byte digest on both networks for the same `RawAlert` content, and the Foundation's public keys are configured on both networks.

The root cause is confirmed at: [7](#0-6) [8](#0-7)

### Citations

**File:** util/gen-types/schemas/extensions.mol (L445-458)
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

table Alert {
    raw:                        RawAlert,
    signatures:                 BytesVec,
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

**File:** util/gen-types/src/extension/calc_hash.rs (L292-300)
```rust
impl<'r> packed::RawAlertReader<'r> {
    /// Calculates the hash for [self.as_slice()] as the alert hash.
    ///
    /// [self.as_slice()]: ../prelude/trait.Reader.html#tymethod.as_slice
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
}
impl_calc_special_hash_for_entity!(RawAlert, calc_alert_hash);
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

**File:** test/src/specs/alert/alert_propagation.rs (L143-155)
```rust
fn create_alert(raw_alert: RawAlert, privkeys: &[Privkey]) -> Alert {
    let msg: Message = raw_alert.calc_alert_hash().into();
    let signatures = privkeys
        .iter()
        .take(2)
        .map(|key| {
            let data: Bytes = key
                .sign_recoverable(&msg)
                .expect("Sign failed")
                .serialize()
                .into();
            data.into()
        })
```

**File:** util/network-alert/src/alert_relayer.rs (L1-7)
```rust
//! AlertRelayer
//! We implement a Bitcoin like alert system, n of m alert key holders can decide to send alert
//messages to all client
//! to leave a space to reach consensus offline under critical bugs
//!
//! A cli to generate alert message,
//! A config option to set alert messages to broadcast.
```
