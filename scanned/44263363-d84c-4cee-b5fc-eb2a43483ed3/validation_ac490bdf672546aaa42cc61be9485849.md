### Title
Network Alert Signature Lacks Chain-Domain Binding, Enabling Cross-Network Replay — (`util/network-alert/src/verifier.rs`)

---

### Summary

The CKB Network Alert system signs and verifies alerts using only the hash of the `RawAlert` struct. Because `RawAlert` contains no chain identifier or genesis hash, a valid alert signed for one CKB network (e.g., testnet) passes signature verification on any other CKB network (e.g., mainnet) that uses the same hard-coded public keys. An unprivileged attacker who observes a legitimately signed alert on one network can replay it on another via the `send_alert` RPC, causing false emergency messages to propagate across the entire mainnet P2P network.

---

### Finding Description

**Vulnerability class:** Missing domain/chain separation in a signed message — direct analog to the Connext H-06 finding where signed calldata lacked a destination-domain field.

**`RawAlert` molecule schema** (`util/gen-types/schemas/extensions.mol`, lines 445–453):

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

There is no `chain_id`, `genesis_hash`, or any network-specific field. [1](#0-0) 

**`calc_alert_hash`** computes `blake2b_256(raw_alert.as_slice())` — a pure hash of the `RawAlert` bytes with no domain separation: [2](#0-1) 

**`Verifier::verify_signatures`** derives the message to verify as `Message::from_slice(alert.calc_alert_hash().as_slice())` and checks it against the configured public keys. No chain context is consulted: [3](#0-2) 

**The same four public keys are hard-coded** in `alert_signature.toml` and loaded as `NetworkAlertConfig::default()` for all networks: [4](#0-3) 

```toml
signatures_threshold = 2
public_keys = [
  "0x03933a9b116c5017561742c37ae69acb0dca3329a52c479c85df5bb387c8ac8715",
  "0x038da23240e5a4234601902cf3db3cdfc1b3fdb2db2a54ba6204f6ca1d6ef6129a",
  "0x02adc94b64a9809019139fe70bd26aa0d787772a1ad645a4bcb1456fb3e1105f09",
  "0x0369eca725513fc94685cd0b8ccebc7be874afea38d23ccb090566aa1c50d696b1",
]
```

`NetworkAlertConfig::default()` loads this same file for every network: [5](#0-4) 

The `send_alert` RPC entry point accepts an alert, checks only that `notice_until` is in the future, verifies signatures, and then broadcasts to all connected peers: [6](#0-5) 

The P2P `AlertRelayer::received` handler also verifies signatures and rebroadcasts without any chain-domain check: [7](#0-6) 

---

### Impact Explanation

An attacker who obtains a legitimately signed testnet alert (by observing the testnet P2P network) can submit it to any mainnet node's `send_alert` RPC. Because:

1. The `calc_alert_hash()` is identical on both networks (no chain ID in `RawAlert`),
2. The same four developer public keys are the default on all networks,
3. The `notice_until` timestamp is still in the future,

the mainnet node accepts the alert as valid, stores it, and rebroadcasts it to all connected mainnet peers. Every mainnet node operator sees a false emergency message. If the alert message says "upgrade immediately" or "critical bug found," this can cause mass unnecessary upgrades, panic, or loss of trust in the alert system. The alert system is explicitly described as being used for "urgent situations" and "critical problems."

---

### Likelihood Explanation

- The `send_alert` RPC is reachable by any caller with RPC access to a mainnet node (it is a standard, documented RPC module).
- Testnet alerts are observable on the public testnet P2P network by any participant.
- The same default public keys are used on all networks unless operators explicitly override `alert_signature` in `ckb.toml`.
- No special privilege is required: the attacker only needs to observe a testnet alert and call `send_alert` on a mainnet node.
- The P2P identify protocol (`/{chain_id}/{genesis_hash[..8]}`) prevents direct cross-network P2P connections, but the RPC path bypasses this entirely.

---

### Recommendation

Include a chain-specific domain separator in the signed message. The simplest fix is to include the genesis block hash in the data that is hashed for signing:

1. Add a `chain_hash: Byte32` field to `RawAlert` in `util/gen-types/schemas/extensions.mol`, or
2. Compute the alert hash as `blake2b_256(genesis_hash || raw_alert.as_slice())` inside `calc_alert_hash`, where `genesis_hash` is passed as a parameter from the consensus context.

The `Verifier` should be initialized with the local chain's genesis hash and incorporate it into the message digest before calling `verify_m_of_n`. This ensures a signature produced for testnet is cryptographically invalid on mainnet.

---

### Proof of Concept

1. Run a CKB testnet node and a CKB mainnet node (both with `Alert` RPC enabled and default `alert_signature` config).
2. On testnet, call `send_alert` with a crafted alert signed by 2-of-4 developer keys (or observe a real one propagating on testnet P2P).
3. Copy the exact alert JSON (including `id`, `notice_until`, `message`, `signatures`).
4. Submit the identical alert JSON to the mainnet node's `send_alert` RPC.
5. Observe that the mainnet node accepts it (`result: null`), stores it, and broadcasts it to all connected mainnet peers.
6. Query `get_blockchain_info` on any mainnet peer — the false alert appears in the `alerts` field.

The root cause is confirmed at:
- `util/gen-types/schemas/extensions.mol` lines 445–453: `RawAlert` has no chain field. [1](#0-0) 
- `util/gen-types/src/extension/calc_hash.rs` lines 296–298: hash is over raw bytes only. [8](#0-7) 
- `util/network-alert/src/verifier.rs` line 35: message derived from chain-agnostic hash. [9](#0-8) 
- `util/app-config/src/configs/network_alert.rs` lines 14–18: same keys default on all networks. [5](#0-4)

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

**File:** util/app-config/src/configs/network_alert.rs (L14-18)
```rust
impl Default for Config {
    fn default() -> Self {
        let alert_config = include_bytes!("./alert_signature.toml");
        toml::from_slice(&alert_config[..]).expect("alert system config")
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
