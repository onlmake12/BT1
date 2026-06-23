### Title
Network Alert `RawAlert` Hash Missing Chain Identifier Enables Cross-Network Replay of Signed Alerts - (File: `util/network-alert/src/verifier.rs`)

---

### Summary

The `RawAlert` hash used for multi-signature verification in CKB's network alert system does not include any chain-specific identifier (chain ID, genesis hash, or network name). Because the same developer public keys are hard-coded across all CKB networks, a valid alert signed for testnet can be replayed on mainnet by any unprivileged P2P peer, causing false alert messages to be accepted, displayed to users, and re-broadcast across the entire mainnet P2P network.

---

### Finding Description

**Root cause — missing domain separator in the signing hash:**

`Verifier::verify_signatures` computes the signing message as `alert.calc_alert_hash()`:

```rust
// util/network-alert/src/verifier.rs
pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
    let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
    // ...
    verify_m_of_n(&message, self.config.signatures_threshold, &signatures, &self.pubkeys)
}
```

`calc_alert_hash` is a plain `blake2b` over the raw bytes of `RawAlert`:

```rust
// util/gen-types/src/extension/calc_hash.rs
impl<'r> packed::RawAlertReader<'r> {
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()   // blake2b(self.as_slice())
    }
}
```

The `RawAlert` molecule schema contains only:

```
// util/gen-types/schemas/extensions.mol
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

No chain ID, genesis block hash, or network name is present. The hash is therefore identical for the same `RawAlert` content regardless of which CKB network it was signed for.

**Same keys across networks:**

The public keys are hard-coded in `NetworkAlertConfig` and are the same for mainnet and testnet (confirmed by `test_alert_20230001` which uses `NetworkAlertConfig::default()` and verifies real mainnet signatures). A signature valid on testnet is cryptographically valid on mainnet.

**P2P handler accepts alerts without checking `notice_until`:**

The `AlertRelayer::received` P2P handler verifies signatures but performs no expiry check:

```rust
// util/network-alert/src/alert_relayer.rs
if let Err(err) = self.verifier.verify_signatures(&alert) {
    nc.ban_peer(...);
    return;
}
// mark sender as known
self.mark_as_known(peer_index, alert_id);
// broadcast message
nc.quick_filter_broadcast(..., data);
// add to received alerts
self.notifier.lock().add(&alert);
```

The `notice_until` check exists only in the `send_alert` RPC handler, not in the P2P path. An attacker replaying via P2P bypasses this check entirely.

---

### Impact Explanation

An unprivileged peer who captures a valid testnet alert (signed by CKB developers) can replay it on mainnet via the P2P Alert protocol. Mainnet nodes will:
1. Verify the signatures — **pass** (same keys, same hash)
2. Accept the alert and add it to their notifier
3. Re-broadcast it to all connected mainnet peers

The result is a false, developer-signed alert message propagated across the entire mainnet P2P network and displayed to all mainnet users. The alert system is specifically designed to warn users about critical bugs; a false alert can cause unnecessary panic, prompt users to stop using the network, or trigger unnecessary software upgrades. Because the alert is re-broadcast by every accepting node, a single replayed packet fans out to the entire network.

---

### Likelihood Explanation

The attack requires no privileged access:
- **Step 1**: Passively monitor the CKB testnet P2P network for alert messages (public network, no credentials needed).
- **Step 2**: Capture a valid alert with a future `notice_until` timestamp.
- **Step 3**: Connect to CKB mainnet as a normal P2P peer and send the captured alert bytes via the Alert protocol.

The only prerequisite is that the CKB team has issued at least one testnet alert with a future expiry — a realistic condition given historical testnet alerts (e.g., `test_alert_20230001` shows a real alert was issued in 2023). The attack is passive until that condition is met, then trivially executable.

---

### Recommendation

Include a chain-specific identifier in the `RawAlert` hash computation. The genesis block hash (available at node startup) should be mixed into the hash before signing and verification:

```rust
// In calc_alert_hash or in Verifier::verify_signatures:
let mut blake2b = new_blake2b();
blake2b.update(genesis_hash.as_slice());   // chain-specific domain separator
blake2b.update(raw_alert.as_slice());
blake2b.finalize(&mut result);
```

Alternatively, add a `chain_id` or `genesis_hash` field to `RawAlert` itself so it is covered by the signature.

---

### Proof of Concept

```
1. Run a CKB testnet node and monitor the Alert P2P protocol (protocol_id for Alert).
2. Wait for the CKB team to issue a testnet alert (or use the known alert from 2023
   with id=20230001, notice_until=1681574400000).
3. Capture the raw alert bytes from the testnet P2P stream.
4. Connect to a CKB mainnet node as a normal P2P peer.
5. Send the captured alert bytes via the Alert protocol.
6. The mainnet node calls verifier.verify_signatures(&alert):
   - Computes blake2b(raw_alert.as_slice()) — identical to testnet hash
   - Verifies 2-of-4 signatures against hard-coded mainnet keys — PASS
7. The mainnet node adds the alert to its notifier and broadcasts it to all peers.
8. All mainnet nodes display the testnet alert message to their operators/users.
```

**Key files:**
- [1](#0-0) 
- [2](#0-1) 
- [3](#0-2) 
- [4](#0-3)

### Citations

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

**File:** util/network-alert/src/alert_relayer.rs (L150-178)
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
        // mark sender as known
        self.mark_as_known(peer_index, alert_id);
        // broadcast message
        let selected_peers: Vec<PeerIndex> = nc
            .connected_peers()
            .into_iter()
            .filter(|peer| self.mark_as_known(*peer, alert_id))
            .collect();
        if let Err(err) = nc.quick_filter_broadcast(
            TargetSession::Multi(Box::new(selected_peers.into_iter())),
            data,
        ) {
            debug!("alert broadcast error: {:?}", err);
        }
        // add to received alerts
        self.notifier.lock().add(&alert);
```
