### Title
NetworkAlert Signed Digest Lacks Chain Domain Separator, Enabling Cross-Network Replay — (`util/network-alert/src/verifier.rs`)

### Summary
`RawAlert` is hashed without any chain or network identifier. Because the same Nervos Foundation signing keys are hard-coded for every network, a legitimately signed alert from testnet can be replayed verbatim on mainnet (or vice versa) by any unprivileged P2P peer or RPC caller, causing the false alert to propagate to every node on the target network.

### Finding Description

`calc_alert_hash()` is a plain blake2b hash of the raw molecule-encoded `RawAlert` bytes with no domain prefix: [1](#0-0) 

The `RawAlert` molecule schema contains only application-level fields — `notice_until`, `id`, `cancel`, `priority`, `message`, `min_version`, `max_version` — with no chain ID, genesis hash, or any other network-scoping field: [2](#0-1) 

`Verifier::verify_signatures()` derives the message to verify directly from this domain-free hash: [3](#0-2) 

The default `NetworkAlertConfig` loads a single `alert_signature.toml` that hard-codes the same four Foundation public keys for every network (mainnet, testnet, devnet): [4](#0-3) [5](#0-4) 

Because the digest is identical across networks, any `(raw_alert_bytes, signatures)` pair that passes verification on one network passes on every other network that shares the same trusted-key set.

### Impact Explanation

An attacker who observes a valid signed alert on testnet (alerts are broadcast over P2P and are publicly visible) can submit the exact same bytes to mainnet nodes. The `Verifier` accepts it, the `AlertRelayer` stores it and re-broadcasts it to all connected peers, and the alert propagates network-wide:

- **P2P path** (`AlertRelayer::received`): no `notice_until` check; any connected peer can inject the replayed alert directly.
- **RPC path** (`send_alert`): rejects alerts with `notice_until` in the past, but any testnet alert with a future expiry (or one crafted with a far-future expiry) bypasses this check. [6](#0-5) [7](#0-6) 

The result is that every mainnet node displays a false critical-security alert, potentially causing mass user panic, incorrect upgrade actions, or loss of trust in the alert system.

### Likelihood Explanation

- The same four Foundation keys are compiled into every CKB binary regardless of network.
- Historical signed alerts exist on-chain and in public repositories (e.g., alert `20230001` with real signatures is present in the test suite).
- The attacker needs no private key — only the ability to connect as a P2P peer or call the `send_alert` RPC, both of which are available to any unprivileged party.
- The P2P entry point requires no authentication whatsoever.

### Recommendation

Introduce a network-scoped domain separator into the alert digest. At minimum, prepend the genesis block hash (already available as `consensus.genesis_hash`) to the data hashed in `calc_alert_hash()`, so that a signature produced for one network is cryptographically invalid on any other network.

### Proof of Concept

1. On testnet, observe a broadcast `Alert` message (or retrieve alert `20230001` from the test fixture in `util/network-alert/src/tests/generate_alert_signature.rs`).
2. Connect to a mainnet node as a P2P peer and send the raw alert bytes over the `Alert` protocol sub-stream.
3. The mainnet `AlertRelayer::received()` parses the alert, calls `verifier.verify_signatures()`, which recomputes `blake2b(raw_alert_bytes)` — identical to the testnet digest — and recovers the Foundation public keys. Verification succeeds.
4. The alert is stored in the notifier and re-broadcast to all connected mainnet peers, propagating the false alert across the entire mainnet. [8](#0-7) [9](#0-8)

### Citations

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

**File:** util/app-config/src/configs/network_alert.rs (L14-18)
```rust
impl Default for Config {
    fn default() -> Self {
        let alert_config = include_bytes!("./alert_signature.toml");
        toml::from_slice(&alert_config[..]).expect("alert system config")
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

**File:** util/network-alert/src/alert_relayer.rs (L98-179)
```rust
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
