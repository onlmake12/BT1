### Title
Zero `signatures_threshold` Bypasses All Alert Signature Verification - (`File: util/multisig/src/secp256k1.rs`, `util/network-alert/src/verifier.rs`)

---

### Summary

`verify_m_of_n` in `util/multisig/src/secp256k1.rs` does not validate that `m_threshold > 0`. When `NetworkAlertConfig.signatures_threshold` is set to `0` — which is never rejected during config loading or `Verifier` initialization — any alert with an empty signatures list passes verification unconditionally. An unprivileged P2P peer or RPC caller can then inject and broadcast unsigned alerts across the network.

---

### Finding Description

`Verifier::new` in `util/network-alert/src/verifier.rs` accepts any `NetworkAlertConfig` without checking that `signatures_threshold` is non-zero: [1](#0-0) 

`verify_signatures` passes `self.config.signatures_threshold` directly to `verify_m_of_n`: [2](#0-1) 

Inside `verify_m_of_n`, the two early-exit guards are:

```rust
if sigs.len() > pks.len() { ... }   // 0 > n  → false
if m_threshold > sigs.len() { ... } // 0 > 0  → false
```

The final check is:

```rust
if verified_sig_count < m_threshold { ... } // 0 < 0 → false
``` [3](#0-2) 

All three guards are vacuously satisfied when `m_threshold = 0` and `sigs = []`. The function returns `Ok(())` without recovering or verifying a single signature.

`NetworkAlertConfig` has no invariant enforcement: [4](#0-3) 

The `alert_signature` field in `CKBAppConfig` is `Option<NetworkAlertConfig>`. When absent it falls back to `unwrap_or_default()`, which loads the hardcoded safe config (threshold=2, 4 pubkeys). But when a user supplies a custom section with `signatures_threshold = 0`, no validation rejects it: [5](#0-4) [6](#0-5) 

---

### Impact Explanation

With `signatures_threshold = 0` in `ckb.toml`, any P2P peer can send a `packed::Alert` with an empty `signatures` field. `AlertRelayer::received` calls `self.verifier.verify_signatures(&alert)`, which returns `Ok(())`, causing the node to:

1. Accept the alert as valid.
2. Broadcast it to all connected peers via `quick_filter_broadcast`.
3. Store it in the notifier and surface it to subscribers (including the `network_alert_notify_script` hook). [7](#0-6) 

This allows an unprivileged peer to inject arbitrary alert messages — including fake "critical bug" notices — that propagate network-wide, causing user confusion, potential panic, and disruption of the alert system's integrity.

---

### Likelihood Explanation

The default config is safe (hardcoded `alert_signature.toml` with threshold=2). However, the `alert_signature` block is an overridable `Option` in `ckb.toml`. A node operator deploying on a new network, testnet, or custom chain spec may supply a custom `alert_signature` section and accidentally set `signatures_threshold = 0` (e.g., intending to disable alerts, or during testing). No startup validation catches this. The analogy to the original report is exact: the critical security parameter is not validated at initialization time. [8](#0-7) 

---

### Recommendation

1. **In `Verifier::new`**: assert or return an error if `config.signatures_threshold == 0` or `config.signatures_threshold > config.public_keys.len()`.
2. **In `verify_m_of_n`**: add an explicit guard at the top: `if m_threshold == 0 { return Err(ErrorKind::InvalidThreshold.into()); }`.
3. **In config loading**: validate `NetworkAlertConfig` fields after deserialization, rejecting a zero threshold before the node starts. [1](#0-0) [9](#0-8) 

---

### Proof of Concept

**`ckb.toml` (attacker-controlled node or misconfigured node):**
```toml
[alert_signature]
signatures_threshold = 0
public_keys = []
```

**Attacker sends via RPC or P2P:**
```json
{
  "id": "0x1",
  "cancel": "0x0",
  "priority": "0x1",
  "message": "FAKE: CKB has a critical bug, stop all transactions immediately!",
  "notice_until": "0x99999999999",
  "signatures": []
}
```

**Execution trace:**
1. `Verifier::new` builds `pubkeys = HashSet {}`, `signatures_threshold = 0` — no panic, no error.
2. `verify_signatures` collects `signatures: Vec<Signature> = []`.
3. `verify_m_of_n(message, 0, &[], &{})`:
   - `0 > 0` → false (no `SigCountOverflow`)
   - `0 > 0` → false (no `SigNotEnough`)
   - `verified_sig_count = 0`, `0 < 0` → false (no `Threshold` error)
   - Returns `Ok(())`
4. Alert is stored and broadcast to all peers. [3](#0-2) [10](#0-9)

### Citations

**File:** util/network-alert/src/verifier.rs (L22-29)
```rust
    pub fn new(config: NetworkAlertConfig) -> Self {
        let pubkeys = config
            .public_keys
            .iter()
            .map(|raw| Pubkey::from_slice(raw.as_bytes()))
            .collect::<Result<HashSet<Pubkey>, _>>()
            .expect("builtin pubkeys");
        Verifier { config, pubkeys }
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

**File:** util/multisig/src/secp256k1.rs (L11-54)
```rust
pub fn verify_m_of_n<S>(
    message: &Message,
    m_threshold: usize,
    sigs: &[Signature],
    pks: &HashSet<Pubkey, S>,
) -> Result<(), Error>
where
    S: BuildHasher,
{
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

**File:** util/app-config/src/configs/network_alert.rs (L7-12)
```rust
pub struct Config {
    /// The minimum number of required signatures to send a network alert.
    pub signatures_threshold: usize,
    /// The public keys of all the network alert signers.
    pub public_keys: Vec<JsonBytes>,
}
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

**File:** util/app-config/src/app_config.rs (L87-88)
```rust
    /// P2P alert config options.
    pub alert_signature: Option<NetworkAlertConfig>,
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
