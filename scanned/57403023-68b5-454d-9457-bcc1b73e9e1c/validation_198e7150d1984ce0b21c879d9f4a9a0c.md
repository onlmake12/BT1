### Title
Network Alert Signed Data Contains No Network Identifier, Enabling Cross-Network Replay — (`util/network-alert/src/verifier.rs`, `util/gen-types/schemas/extensions.mol`)

---

### Summary

The CKB network alert system signs a `RawAlert` message whose hash contains no network or chain identifier. A valid alert signature produced for one CKB network (e.g., testnet) is cryptographically indistinguishable from a valid alert on any other CKB network (e.g., mainnet). Any unprivileged peer or RPC caller can replay a legitimately signed alert from one network onto another.

---

### Finding Description

The `RawAlert` molecule schema contains only application-level fields:

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
``` [1](#0-0) 

There is no `network_id`, `genesis_hash`, or any other chain-scoping field. The alert hash is computed as a plain `blake2b_256` of the raw alert bytes:

```rust
impl<'r> packed::RawAlertReader<'r> {
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()   // = blake2b_256(self.as_slice())
    }
}
``` [2](#0-1) 

The verifier signs and verifies only this hash:

```rust
pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
    let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
    // ... verifies signatures against message with no network context
``` [3](#0-2) 

Because the same set of Nervos Foundation signing keys is used across all CKB networks (mainnet, testnet, devnet), a `(RawAlert, signatures)` pair that is valid on testnet is also valid on mainnet — the signature verification passes identically on both.

The two externally reachable entry paths are:

1. **RPC `send_alert`** — any RPC caller can submit a replayed alert directly.
2. **P2P alert relay** — any connected peer can broadcast a replayed alert; `alert_relayer.rs` calls `verify_signatures` and, on success, re-broadcasts to all connected peers. [4](#0-3) [5](#0-4) 

---

### Impact Explanation

An attacker who observes a legitimately signed alert on CKB testnet (or any other CKB network) can immediately replay it on CKB mainnet. The replayed alert will pass `verify_signatures`, be stored by the notifier, and be re-broadcast to all mainnet peers. This can:

- Inject false emergency messages onto mainnet (e.g., a testnet "upgrade required" alert replayed on mainnet with a different version range context).
- Cancel a live mainnet alert by replaying a testnet cancel-alert with the same `cancel` field value.
- Cause widespread user confusion and unnecessary node upgrades or shutdowns.

The `cancel` field in `RawAlert` means a replayed alert can actively suppress a legitimate mainnet alert if the IDs match.

---

### Likelihood Explanation

All CKB networks share the same hardcoded signing public keys. Any valid alert ever issued on testnet is permanently replayable on mainnet. The replay requires no private key material — only the public alert bytes, which are broadcast over P2P and observable by any node. The `send_alert` RPC endpoint is accessible to any local or remote RPC caller with network access.

---

### Recommendation

Include a network-scoping field in the signed alert data. The most robust approach is to include the genesis block hash of the target network in `RawAlert`:

```
table RawAlert {
    notice_until:   Uint64,
    id:             Uint32,
    cancel:         Uint32,
    priority:       Uint32,
    message:        Bytes,
    min_version:    BytesOpt,
    max_version:    BytesOpt,
    genesis_hash:   Byte32,   // <-- add this
}
```

`verify_signatures` should then additionally assert that `alert.raw().genesis_hash()` matches the local node's genesis hash before accepting the alert.

---

### Proof of Concept

1. Run a CKB testnet node and a CKB mainnet node.
2. Issue a valid alert on testnet via `send_alert` RPC (signed by the Nervos Foundation testnet keys, which are the same as mainnet keys).
3. Capture the raw `Alert` bytes from the testnet P2P broadcast.
4. Submit the identical `Alert` bytes to the mainnet node via `send_alert` RPC or inject via P2P.
5. Observe that `verify_signatures` succeeds on mainnet — the alert is stored and re-broadcast to all mainnet peers — because `calc_alert_hash()` produces the same value on both networks and the signing keys are identical. [6](#0-5) [7](#0-6)

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

**File:** util/network-alert/src/alert_relayer.rs (L150-162)
```rust
        // verify
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

**File:** rpc/src/module/alert.rs (L102-131)
```rust
    fn send_alert(&self, alert: Alert) -> Result<()> {
        let alert: packed::Alert = alert.into();
        let now_ms = ckb_systemtime::unix_time_as_millis();
        let notice_until: u64 = alert.raw().notice_until().into();
        if notice_until < now_ms {
            return Err(RPCError::invalid_params(format!(
                "Expected `params[0].notice_until` in the future (> {now_ms}), got {notice_until}",
            )));
        }

        let result = self.verifier.verify_signatures(&alert);

        match result {
            Ok(()) => {
                // set self node notifier
                self.notifier.lock().add(&alert);

                self.network_controller.broadcast_with_handle(
                    SupportProtocols::Alert.protocol_id(),
                    alert.as_bytes(),
                    &self.handle,
                );
                Ok(())
            }
            Err(e) => Err(RPCError::custom_with_error(
                RPCError::AlertFailedToVerifySignatures,
                e,
            )),
        }
    }
```
