### Title
Cross-Network Alert Signature Replay in `RawAlert` Hash — (`util/network-alert/src/verifier.rs`)

### Summary

The `RawAlert` message structure contains no chain-specific identifier (genesis hash, network ID, or any domain separator). The `calc_alert_hash()` function hashes only the raw alert content. As a result, a valid multi-signature alert produced by the Nervos Foundation for one CKB network (e.g., testnet) is cryptographically identical and fully valid on any other CKB network (e.g., mainnet), enabling cross-network alert replay by any unprivileged observer.

### Finding Description

The `RawAlert` molecule schema is defined as:

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

There is no `chain_id`, `genesis_hash`, or any network-specific field in `RawAlert`. The alert hash used as the signing message is computed as:

```rust
pub fn calc_alert_hash(&self) -> packed::Byte32 {
    self.calc_hash()   // = blake2b_256(self.as_slice())
}
``` [2](#0-1) 

The `Verifier::verify_signatures()` function derives the signing message exclusively from this chain-agnostic hash:

```rust
pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
    let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
    ...
    verify_m_of_n(&message, self.config.signatures_threshold, &signatures, &self.pubkeys)
        .map_err(|err| err.kind())?;
    Ok(())
}
``` [3](#0-2) 

Because the same Nervos Foundation public keys are hard-coded across all CKB networks and the signed digest contains no network binding, a signed alert observed on testnet produces a byte-for-byte identical valid signature for mainnet (and vice versa), as long as the `RawAlert` fields are the same.

The two entry paths that accept alerts are:

1. **`send_alert` RPC** (`rpc/src/module/alert.rs`): calls `verifier.verify_signatures(&alert)` and, on success, broadcasts the alert to all P2P peers. [4](#0-3) 

2. **P2P `AlertRelayer::received()`** (`util/network-alert/src/alert_relayer.rs`): accepts alert messages from any connected peer, verifies signatures, and re-broadcasts to all connected peers. [5](#0-4) 

Neither path introduces any chain-specific domain separation before or after signature verification.

### Impact Explanation

An attacker who observes a legitimately signed alert on testnet (alerts are broadcast publicly over P2P) can submit that exact alert — with its original signatures — to a mainnet node via the `send_alert` RPC or directly via the P2P protocol. The mainnet `Verifier` will accept it as valid (same keys, same hash, same signatures), and the `AlertRelayer` will propagate it to every connected mainnet peer. This allows an unprivileged attacker to:

- Inject false emergency alerts onto mainnet, causing widespread user panic or unnecessary node shutdowns.
- Replay a cancelled or expired testnet alert onto mainnet where it has not yet been cancelled.
- Undermine the integrity of the alert system, which is the sole mechanism for the Nervos Foundation to warn users of critical protocol bugs.

### Likelihood Explanation

- Testnet alerts are publicly observable by any node running on testnet; no privileged access is required.
- The `send_alert` RPC is reachable by any local RPC caller (a supported attacker profile per scope).
- The P2P alert protocol is reachable by any peer connecting to a mainnet node.
- The same Nervos Foundation signing keys are used across all networks, confirmed by the hard-coded `alert_signature.toml` config loaded by default. [6](#0-5) 
- No additional preconditions (leaked keys, majority hashpower, social engineering) are required.

### Recommendation

Include a chain-specific domain separator in the signed message. The natural choice is the genesis block hash, which uniquely identifies each CKB network. Modify `calc_alert_hash()` (or introduce a new `calc_alert_signing_message()`) to commit to the genesis hash:

```rust
pub fn calc_alert_signing_message(genesis_hash: &Byte32, alert: &packed::RawAlert) -> Byte32 {
    let mut hasher = new_blake2b();
    hasher.update(genesis_hash.as_slice());
    hasher.update(alert.as_slice());
    let mut result = [0u8; 32];
    hasher.finalize(&mut result);
    result.into()
}
```

Pass the genesis hash into `Verifier` at construction time and use this function in `verify_signatures()` instead of `alert.calc_alert_hash()`. Update the signing tooling accordingly.

### Proof of Concept

1. Run a CKB testnet node and a CKB mainnet node.
2. On testnet, observe (or cause) a valid signed alert to be broadcast (e.g., via `send_alert` RPC with valid testnet signatures). Capture the raw `Alert` bytes from the P2P stream.
3. On mainnet, call `send_alert` RPC with the identical alert payload (same `id`, `message`, `notice_until`, `signatures`).
4. The mainnet `Verifier::verify_signatures()` computes `blake2b_256(raw_alert_bytes)` — identical to testnet — and accepts the signatures as valid.
5. The mainnet `AlertRelayer` broadcasts the false alert to all connected mainnet peers, which propagate it further.

The root cause is confirmed at:
- Schema: `util/gen-types/schemas/extensions.mol` lines 445–453 (no chain field in `RawAlert`)
- Hash: `util/gen-types/src/extension/calc_hash.rs` lines 296–298 (hash is purely content-based)
- Verification: `util/network-alert/src/verifier.rs` lines 33–64 (no chain binding in message)

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

**File:** util/network-alert/src/alert_relayer.rs (L97-179)
```rust
    #[allow(clippy::needless_collect)]
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        data: Bytes,
    ) {
        let alert: packed::Alert = match packed::AlertReader::from_slice(&data) {
            Ok(alert) => {
                if alert.raw().message().is_utf8()
                    && alert
                        .raw()
                        .min_version()
                        .to_opt()
                        .map(|x| x.is_utf8())
                        .unwrap_or(true)
                    && alert
                        .raw()
                        .max_version()
                        .to_opt()
                        .map(|x| x.is_utf8())
                        .unwrap_or(true)
                {
                    alert.to_entity()
                } else {
                    info!(
                        "A malformed message fromP peer {} : not utf-8 string",
                        peer_index
                    );
                    nc.ban_peer(
                        peer_index,
                        BAD_MESSAGE_BAN_TIME,
                        String::from("send us a malformed message: not utf-8 string"),
                    );
                    return;
                }
            }
            Err(err) => {
                info!("A malformed message from peer {}: {:?}", peer_index, err);
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };
        let alert_id = alert.as_reader().raw().id().into();
        trace!("ReceiveD alert {} from peer {}", alert_id, peer_index);
        // ignore alert
        if self.notifier.lock().has_received(alert_id) {
            return;
        }
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
    }
```

**File:** util/app-config/src/configs/network_alert.rs (L14-18)
```rust
impl Default for Config {
    fn default() -> Self {
        let alert_config = include_bytes!("./alert_signature.toml");
        toml::from_slice(&alert_config[..]).expect("alert system config")
    }
```
