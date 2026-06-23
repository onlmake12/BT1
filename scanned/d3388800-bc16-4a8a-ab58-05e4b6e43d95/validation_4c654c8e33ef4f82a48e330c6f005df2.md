### Title
Missing `signatures_threshold > 0` Guard in `Verifier::verify_signatures()` Allows Unauthenticated Alert Injection — (File: `util/network-alert/src/verifier.rs`)

---

### Summary

`Verifier::verify_signatures()` delegates to `verify_m_of_n()` using the operator-supplied `signatures_threshold` without first asserting that the threshold is non-zero. When `signatures_threshold = 0`, `verify_m_of_n` returns `Ok(())` for any alert carrying zero signatures, bypassing the entire multi-signature security module. An unprivileged P2P peer can then inject arbitrary alert messages that are accepted, stored, and re-broadcast to all connected peers.

---

### Finding Description

**Root cause — `verify_m_of_n` with `m_threshold = 0`**

`util/multisig/src/secp256k1.rs`, lines 20–53:

```rust
pub fn verify_m_of_n<S>(
    message: &Message,
    m_threshold: usize,
    sigs: &[Signature],
    pks: &HashSet<Pubkey, S>,
) -> Result<(), Error> {
    if sigs.len() > pks.len() {          // (A)
        return Err(ErrorKind::SigCountOverflow.into());
    }
    if m_threshold > sigs.len() {        // (B)
        return Err(ErrorKind::SigNotEnough.into());
    }
    // ... recover and count valid sigs ...
    if verified_sig_count < m_threshold { // (C)
        return Err(ErrorKind::Threshold { ... }.into());
    }
    Ok(())
}
```

When `m_threshold = 0` and `sigs = []`:
- **(A)** `0 > pks.len()` → `false` (passes)
- **(B)** `0 > 0` → `false` (passes)
- **(C)** `0 < 0` → `false` (passes)
- Returns `Ok(())` — **no signature ever checked**

**Missing guard in `Verifier::verify_signatures()`**

`util/network-alert/src/verifier.rs`, lines 33–64:

```rust
pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
    // ...
    verify_m_of_n(
        &message,
        self.config.signatures_threshold,   // ← never checked > 0
        &signatures,
        &self.pubkeys,
    )
    .map_err(|err| err.kind())?;
    Ok(())
}
```

Neither `Verifier::new()` nor `verify_signatures()` asserts `signatures_threshold > 0`. The `NetworkAlertConfig` struct (`util/app-config/src/configs/network_alert.rs`, line 9) declares `signatures_threshold: usize` with no range constraint.

**Trigger path in the launcher**

`util/launcher/src/lib.rs`, line 478:

```rust
let alert_signature_config = self.args.config.alert_signature.clone().unwrap_or_default();
```

If the operator sets `alert_signature.signatures_threshold = 0` in `ckb.toml`, the `AlertRelayer` is constructed with a `Verifier` whose threshold is 0.

**Handler accepts and re-broadcasts the alert**

`util/network-alert/src/alert_relayer.rs`, lines 151–178:

```rust
if let Err(err) = self.verifier.verify_signatures(&alert) {
    // ban peer and return
}
// mark sender as known
// broadcast to all connected peers
self.notifier.lock().add(&alert);
```

With threshold 0, `verify_signatures` returns `Ok(())` for any alert with zero signatures. The alert is stored in the notifier and re-broadcast to every connected peer.

---

### Impact Explanation

An unprivileged P2P peer can inject arbitrary network alert messages — including fake "critical bug" notices — that are accepted by the target node and immediately re-broadcast to all of its peers. The `Notifier` stores the alert and delivers it to any registered `network_alert_notify_script`, potentially triggering operator-configured automation. Network-wide spam of fabricated alerts can cause panic, disrupt operations, or be used as a distraction during other attacks. The impact is analogous to the original report: a missing guard on the security module configuration allows unauthorized messages to be accepted and propagated.

---

### Likelihood Explanation

The condition requires `signatures_threshold = 0` in the node's `ckb.toml`. This is not the default (default is 2 with 4 hardcoded Nervos Foundation keys), but the field is an unconstrained `usize` with no startup validation, making it a realistic misconfiguration. The severity is Medium, matching the original report's classification: the vulnerability is only active under a specific (non-default) configuration state, but once active it is trivially exploitable by any peer with a TCP connection.

---

### Recommendation

Add a guard in `Verifier::new()` (or `verify_signatures()`) that rejects a zero threshold:

```rust
pub fn new(config: NetworkAlertConfig) -> Self {
    assert!(
        config.signatures_threshold > 0,
        "signatures_threshold must be > 0"
    );
    // ...
}
```

Alternatively, add the check at node startup in `sanitize_block_assembler_config` / the launcher, rejecting a `NetworkAlertConfig` with `signatures_threshold == 0` before the `AlertRelayer` is constructed.

---

### Proof of Concept

1. Set in `ckb.toml`:
   ```toml
   [alert_signature]
   signatures_threshold = 0
   public_keys = []
   ```
2. Start the CKB node.
3. Connect as a P2P peer supporting the Alert protocol.
4. Send a well-formed `packed::Alert` with an empty `signatures` field and any `raw` content (arbitrary `message`, `id`, `notice_until` in the future).
5. The node's `AlertRelayer::received()` calls `verify_signatures()` → `verify_m_of_n(..., 0, [], &{})` → `Ok(())`.
6. The alert is stored in the `Notifier` and broadcast to all connected peers. Any `network_alert_notify_script` configured on the node is executed with the attacker-controlled message string.

**Relevant code locations:**
- [1](#0-0) 
- [2](#0-1) 
- [3](#0-2) 
- [4](#0-3) 
- [5](#0-4)

### Citations

**File:** util/multisig/src/secp256k1.rs (L20-54)
```rust
    if sigs.len() > pks.len() {
        return Err(ErrorKind::SigCountOverflow.into());
    }
    if m_threshold > sigs.len() {
        return Err(ErrorKind::SigNotEnough.into());
    }

    let mut used_pks: HashSet<Pubkey> = HashSet::with_capacity(m_threshold);
    let verified_sig_count = sigs
        .iter()
        .filter_map(|sig| {
            trace!(
                "Recover sig {:x?} with message {:x?}",
                &sig.serialize()[..],
                message.as_ref()
            );
            match sig.recover(message) {
                Ok(pubkey) => Some(pubkey),
                Err(err) => {
                    debug!("recover secp256k1 sig error: {}", err);
                    None
                }
            }
        })
        .filter(|rec_pk| pks.contains(rec_pk) && used_pks.insert(rec_pk.to_owned()))
        .take(m_threshold)
        .count();
    if verified_sig_count < m_threshold {
        return Err(ErrorKind::Threshold {
            pass_sigs: verified_sig_count,
            threshold: m_threshold,
        }
        .into());
    }
    Ok(())
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

**File:** util/network-alert/src/alert_relayer.rs (L151-178)
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

**File:** util/launcher/src/lib.rs (L478-483)
```rust
        let alert_signature_config = self.args.config.alert_signature.clone().unwrap_or_default();
        let alert_relayer = AlertRelayer::new(
            self.version.short(),
            shared.notify_controller().clone(),
            alert_signature_config,
        );
```

**File:** util/app-config/src/configs/network_alert.rs (L7-12)
```rust
pub struct Config {
    /// The minimum number of required signatures to send a network alert.
    pub signatures_threshold: usize,
    /// The public keys of all the network alert signers.
    pub public_keys: Vec<JsonBytes>,
}
```
