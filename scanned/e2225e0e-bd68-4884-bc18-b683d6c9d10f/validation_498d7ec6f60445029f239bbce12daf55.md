### Title
Network Alert Signature Replay Across CKB Networks (Mainnet/Testnet) — (`util/network-alert/src/verifier.rs`)

### Summary
The `RawAlert` message that Nervos Foundation key-holders sign contains no network/chain domain separator. Because `calc_alert_hash` hashes only the raw alert payload bytes and `verify_signatures` checks only that the recovered public keys match the hard-coded set, a validly-signed alert from one CKB network (e.g., testnet/Aggron) is cryptographically indistinguishable from the same alert on another CKB network (e.g., mainnet/Lina). Any unprivileged peer or RPC caller who observes a signed alert on one network can replay it verbatim on another.

### Finding Description

**Root cause — no domain separator in the signed message.**

The `RawAlert` molecule schema contains only application-level fields; there is no `network_id`, `genesis_hash`, or any other chain-specific binding: [1](#0-0) 

`calc_alert_hash` is a straight hash of those raw bytes with no prefix or domain tag: [2](#0-1) 

`verify_signatures` derives the message to verify from that same domain-free hash: [3](#0-2) 

Because both mainnet and testnet ship the same hard-coded public keys, a signature that passes on one network passes identically on the other.

**Attacker-controlled entry paths.**

An unprivileged actor can inject a replayed alert via two surfaces:

1. **RPC** — `send_alert` in `rpc/src/module/alert.rs` accepts any `Alert` JSON object, verifies signatures, and immediately broadcasts it to all peers: [4](#0-3) 

2. **P2P** — `AlertRelayer::received` in `util/network-alert/src/alert_relayer.rs` accepts an alert from any connected peer, verifies signatures, and re-broadcasts it: [5](#0-4) 

Both paths perform only signature verification — no network binding check exists anywhere in the pipeline.

### Impact Explanation

An attacker who observes a legitimately-signed alert on testnet (Aggron) can submit it to any mainnet (Lina) node via RPC or P2P. The mainnet node will:
- Accept it as valid (signatures verify correctly against the same hard-coded keys).
- Store it in the notifier and display it to the node operator.
- Broadcast it to all connected mainnet peers, causing network-wide propagation.

The reverse (mainnet alert replayed on testnet) is equally possible. Concrete harms:
- A testnet-only upgrade notice ("please upgrade to fix testnet bug X") floods mainnet nodes, causing unnecessary operator alarm and potential premature upgrades.
- A future alert with version-range restrictions (`min_version`/`max_version`) intended for one network could trigger incorrect behavior on another network's nodes.
- The integrity of the alert channel — the only out-of-band emergency communication mechanism — is undermined: operators cannot trust that an alert was intended for their network.

### Likelihood Explanation

The signed alert bytes are publicly observable on the P2P network and via `get_blockchain_info` RPC. No privileged access is required. The attacker only needs to run a node on one network, observe an alert, and submit it to a node on the other network. This is a low-effort, zero-cost operation.

### Recommendation

Bind the signed message to the specific network by including a domain separator before hashing. The natural choice is the genesis block hash, which is already available in CKB and uniquely identifies each network. Concretely, modify `calc_alert_hash` (or introduce a new `calc_alert_signing_hash`) to prepend the genesis hash to the `RawAlert` bytes before hashing:

```
signing_hash = blake2b(genesis_hash || raw_alert_bytes)
```

This ensures a signature produced for mainnet is cryptographically invalid on testnet and vice versa, directly mirroring the EIP-712 domain separator fix applied in the referenced report.

### Proof of Concept

1. Run a CKB testnet node and a CKB mainnet node (both use the same hard-coded alert public keys).
2. Obtain a validly-signed alert from testnet — either by observing P2P traffic or calling `get_blockchain_info` on a testnet node that has received one.
3. Submit the identical `Alert` object (same `raw` fields, same `signatures`) to the mainnet node via:
   ```json
   {"jsonrpc":"2.0","method":"send_alert","params":[<testnet_alert>],"id":1}
   ```
4. The mainnet node returns `null` (success), stores the alert, and broadcasts it to all connected mainnet peers.
5. All mainnet peers that receive the relayed alert via `AlertRelayer::received` also accept and re-broadcast it, since `verify_signatures` passes on the same hash with the same keys.

The signed alert bytes are fully public; no key material or privileged access is needed.

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

**File:** util/network-alert/src/verifier.rs (L33-35)
```rust
    pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
        trace!("Verifying alert {:?}", alert);
        let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
```

**File:** rpc/src/module/alert.rs (L102-124)
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
