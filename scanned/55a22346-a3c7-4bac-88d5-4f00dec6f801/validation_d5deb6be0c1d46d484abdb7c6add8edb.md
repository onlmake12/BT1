### Title
Network Alert Signature Lacks Domain Separator, Enabling Cross-Network Replay — (File: `util/network-alert/src/verifier.rs`)

### Summary
The CKB network alert system signs and verifies alerts using a hash of the raw alert payload with no network-specific domain separator. A valid alert signature produced for mainnet is cryptographically identical to a valid signature for testnet (or any other CKB network), enabling cross-network replay of alert messages by any unprivileged P2P peer.

### Finding Description
`calc_alert_hash()` is implemented as a plain `blake2b_256` over the serialized `RawAlert` bytes: [1](#0-0) 

```rust
impl<'r, R> CalcHash for R where R: Reader<'r> {
    fn calc_hash(&self) -> packed::Byte32 {
        blake2b_256(self.as_slice()).into()
    }
}
```

`RawAlertReader::calc_alert_hash()` delegates directly to this: [2](#0-1) 

The `RawAlert` molecule schema contains no network-identifying field: [3](#0-2) 

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

No genesis hash, no chain name, no network magic — nothing that distinguishes mainnet from testnet or any private CKB network.

`Verifier::verify_signatures()` uses this hash directly as the ECDSA message: [4](#0-3) 

```rust
pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
    let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
```

The `AlertRelayer::received()` P2P handler calls `verify_signatures` on every inbound alert and, if it passes, re-broadcasts it to all connected peers and stores it in the notifier: [5](#0-4) 

### Impact Explanation
Any alert that was legitimately signed for one CKB network (e.g., mainnet) carries signatures that are equally valid on every other CKB network (testnet, devnet, private chains). An attacker who observes a live mainnet alert over P2P can relay the identical bytes to testnet nodes. Every testnet node will:
1. Pass `verify_signatures` (the signature is cryptographically valid — the hash is the same on both networks).
2. Store the alert in its `Notifier` and surface it to users via `get_blockchain_info`.
3. Re-broadcast it to all its peers, propagating the replayed alert across the entire testnet.

The reverse direction (testnet → mainnet) is equally possible. The practical consequence is that nodes on the target network display a false security alert, potentially triggering unnecessary emergency upgrades or causing user confusion about the state of the network they are actually running.

### Likelihood Explanation
The attack requires no privileged access. Alerts are broadcast over the open P2P network; any node connected to mainnet can observe a live alert. The attacker then connects to testnet and submits the same alert bytes via the `send_alert` RPC or directly over the P2P alert protocol. The `notice_until` timestamp check in `send_alert` (RPC path) requires the alert to not be expired, but the P2P `received()` handler has no such check — it only calls `verify_signatures`. As long as a real alert has been issued on one network and has not yet expired, replay on another network is trivially achievable by any unprivileged peer.

### Recommendation
Include a network-specific domain separator in the signed message. The genesis hash (`consensus.genesis_hash`) is the natural choice in CKB, as it uniquely identifies each network. The signing input should be:

```
blake2b_256( genesis_hash || raw_alert_bytes )
```

This makes a signature produced for mainnet invalid on testnet and vice versa, eliminating the cross-network replay surface. The `Verifier` should be constructed with the genesis hash and incorporate it when computing the message to verify.

### Proof of Concept

1. Run a mainnet-connected CKB node (Node A) and a testnet-connected CKB node (Node B).
2. Wait for (or trigger via `send_alert` RPC on mainnet) a valid mainnet alert to be broadcast. Capture the raw alert bytes from Node A's P2P stream.
3. Submit the captured bytes to Node B via the `send_alert` RPC or by connecting to Node B as a P2P peer and sending the alert over the Alert protocol (`SupportProtocols::Alert`).
4. Observe that Node B's `get_blockchain_info` response now includes the replayed mainnet alert, and that Node B re-broadcasts it to all its testnet peers.

The root cause is confirmed at:
- `util/gen-types/src/extension/calc_hash.rs` lines 292–298 (`calc_alert_hash` = plain `blake2b_256` with no domain separator)
- `util/gen-types/schemas/extensions.mol` lines 445–453 (`RawAlert` schema has no network field)
- `util/network-alert/src/verifier.rs` lines 33–35 (message derived solely from `calc_alert_hash`)
- `util/network-alert/src/alert_relayer.rs` lines 150–178 (P2P handler accepts and re-broadcasts any alert passing signature check)

### Citations

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

**File:** util/network-alert/src/verifier.rs (L33-35)
```rust
    pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
        trace!("Verifying alert {:?}", alert);
        let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
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
