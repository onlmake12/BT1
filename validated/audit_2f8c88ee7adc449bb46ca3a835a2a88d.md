All cited code is confirmed in the repository. The finding is valid.

Audit Report

## Title
Truncated 32-bit Genesis Hash in `identify_name()` Allows Cross-Chain Peers to Bypass Network Identity Filter — (File: `spec/src/consensus.rs`)

## Summary
`Consensus::identify_name()` embeds only the first 8 hex characters (32 bits) of the 256-bit genesis hash into the P2P identity string. Because `Identify::verify()` performs a full string equality check against this already-truncated string, an attacker who crafts a custom chain whose genesis hash shares the same 4-byte prefix passes the identity gate unconditionally. A passing attacker is never banned, can occupy inbound peer slots, and can flood the sync pipeline with messages that consume CPU and memory before failing block validation.

## Finding Description
`identify_name()` at `spec/src/consensus.rs:965–968` constructs the identity string as `"/{id}/{genesis_hash[..8]}"`, discarding 56 of 64 hex characters:

```rust
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    format!("/{}/{}", self.id, &genesis_hash[..8])   // only 32 bits used
}
``` [1](#0-0) 

This truncated string is the sole chain-identity signal exchanged during the P2P handshake. `Identify::verify()` at `network/src/protocols/identify/mod.rs:541–551` performs a strict equality check against it:

```rust
fn verify(&self, data: &[u8]) -> Option<(Flags, String)> {
    let name = reader.name().as_utf8().ok()?.to_owned();
    if self.name != name {
        warn!("... different network identifiers ...");
        return None;
    }
``` [2](#0-1) 

A `None` return triggers a 5-minute ban and disconnect; a `Some(...)` return fully admits the peer: [3](#0-2) 

The exploit path:
1. Set `id = "ckb"` in a custom chain spec (freely configurable).
2. Iterate a mutable genesis field (e.g. `genesis.message`) until `&genesis_hash_hex[..8]` matches the target network prefix. This requires ~2^32 iterations on average (the submitted report incorrectly states 2^16; 8 hex characters = 32 bits, so a specific prefix match requires ~2^32 attempts). Each iteration involves only a few Blake2b operations (cellbase hash → transactions root → block header hash), making this feasible in hours on commodity hardware with a tight loop.
3. Connect to mainnet/testnet peers. `verify()` evaluates `self.name != name` as `false` (both sides produce `"/ckb/<same-8-chars>"`), returns `Some(...)`, and the peer is admitted.
4. From the admitted connection, send a stream of `SendHeaders` messages referencing headers from the custom chain. `HeadersProcess` processes each header through `HeaderAcceptor::accept()` — running `prev_block_check`, `non_contextual_check`, and `version_check` — before rejecting them. This consumes CPU and memory on the victim node before the invalid headers are discarded. [4](#0-3) 

No deeper check re-validates chain identity after the identify handshake. The `BAN_ON_NOT_SAME_NET` path is never triggered for a passing attacker. [5](#0-4) 

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker operating a fleet of such nodes can exhaust the inbound peer slots of mainnet/testnet nodes, preventing legitimate peers from connecting and degrading network connectivity. Simultaneously, admitted attacker nodes can flood the sync pipeline (`HeadersProcess`, `BlockProcess`) with messages that pass the identity gate and consume CPU/memory before failing block validation, causing sustained resource pressure on targeted nodes.

## Likelihood Explanation
- No privileged access is required; any unprivileged network peer can execute this.
- The `id` field in a chain spec is freely settable — no key material or insider knowledge needed.
- The collision search over 32 bits requires ~2^32 genesis hash computations on average (not 2^16 as stated in the submitted report — the correct figure is ~4 billion iterations). Each iteration is a small number of Blake2b calls; on optimized commodity hardware this is feasible in hours, not seconds. The attack is still practical for a motivated attacker.
- The attack is repeatable and scalable across many attacker IPs, and the 5-minute ban is never triggered because the attacker passes, not fails, the identity check.

## Recommendation
Replace the 8-character slice with the full 64-character genesis hash string in `spec/src/consensus.rs`:

```rust
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    format!("/{}/{}", self.id, &genesis_hash)   // full 256-bit hash
}
``` [6](#0-5) 

This is a non-breaking change for honest nodes (all derive the same full hash from the same genesis block) and raises the collision cost from ~2^32 to ~2^128, making the attack computationally infeasible.

## Proof of Concept
1. Clone the CKB repository and create a custom chain spec with `name = "ckb"`.
2. Write a tight loop that increments `genesis.message`, calls `build_consensus()`, formats the genesis hash as hex, and checks whether `&genesis_hash_hex[..8]` matches the mainnet prefix. Expect ~2^32 iterations on average (correcting the submitted report's 2^16 estimate); with optimized Blake2b computation this completes in hours on commodity hardware.
3. Start a CKB node on this custom chain.
4. Connect to a mainnet peer. `Identify::verify()` at `network/src/protocols/identify/mod.rs:545` evaluates `self.name != name` as `false`, returns `Some(...)`, and the peer is admitted.
5. From the admitted connection, send a stream of `SendHeaders` messages referencing headers from the custom chain. Observe that each message enters `HeadersProcess::execute()` and runs through `HeaderAcceptor::accept()` — confirming the identity gate is the only chain-identity barrier and it has been bypassed. [7](#0-6)

### Citations

**File:** spec/src/consensus.rs (L964-968)
```rust
    /// The network identify name, used for network identify protocol
    pub fn identify_name(&self) -> String {
        let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
        format!("/{}/{}", self.id, &genesis_hash[..8])
    }
```

**File:** network/src/protocols/identify/mod.rs (L24-25)
```rust
const MAX_RETURN_LISTEN_ADDRS: usize = 10;
const BAN_ON_NOT_SAME_NET: Duration = Duration::from_secs(5 * 60);
```

**File:** network/src/protocols/identify/mod.rs (L389-397)
```rust
        match self.identify.verify(identify) {
            None => {
                self.network_state.ban_session(
                    &context.control().clone().into(),
                    context.session.id,
                    BAN_ON_NOT_SAME_NET,
                    "The nodes are not on the same network".to_string(),
                );
                MisbehaveResult::Disconnect
```

**File:** network/src/protocols/identify/mod.rs (L541-551)
```rust
    fn verify(&self, data: &[u8]) -> Option<(Flags, String)> {
        let reader = packed::IdentifyReader::from_slice(data).ok()?;

        let name = reader.name().as_utf8().ok()?.to_owned();
        if self.name != name {
            warn!(
                "IdentifyProtocol detects peer has different network identifiers, local network id: {}, remote network id: {}",
                self.name, name,
            );
            return None;
        }
```

**File:** sync/src/synchronizer/headers_process.rs (L94-219)
```rust
    pub fn execute(self) -> Status {
        debug!("HeadersProcess begins");
        let shared: &SyncShared = self.synchronizer.shared();
        let consensus = shared.consensus();
        let headers = self
            .message
            .headers()
            .to_entity()
            .into_iter()
            .map(packed::Header::into_view)
            .collect::<Vec<_>>();

        if headers.len() > MAX_HEADERS_LEN {
            warn!("HeadersProcess is oversized");
            return StatusCode::HeadersIsInvalid.with_context("oversize");
        }

        if headers.is_empty() {
            // Empty means that the other peer's tip may be consistent with our own best known,
            // but empty cannot 100% confirm this, so it does not set the other peer's best header
            // to the shared best known.
            // This action means that if the newly connected node has not been sync with headers,
            // it cannot be used as a synchronization node.
            debug!("HeadersProcess is_empty (synchronized)");
            if let Some(mut state) = self.synchronizer.peers().state.get_mut(&self.peer) {
                self.synchronizer
                    .shared()
                    .state()
                    .tip_synced(state.value_mut());
            }
            return Status::ok();
        }

        if !self.is_continuous(&headers) {
            warn!("HeadersProcess is not continuous");
            return StatusCode::HeadersIsInvalid.with_context("not continuous");
        }

        let result = self.accept_first(&headers[0]);
        match result.state {
            ValidationState::Invalid => {
                debug!(
                    "HeadersProcess accept_first result is invalid, error = {:?}, first header = {:?}",
                    result.error, headers[0]
                );
                return StatusCode::HeadersIsInvalid
                    .with_context(format!("accept first header {:?}", headers[0]));
            }
            ValidationState::TemporaryInvalid => {
                debug!(
                    "HeadersProcess accept_first result is temporary invalid, first header = {:?}",
                    headers[0]
                );
                return Status::ok();
            }
            ValidationState::Valid => {
                // Valid, do nothing
            }
        };

        for header in headers.iter().skip(1) {
            let verifier = HeaderVerifier::new(shared, consensus);
            let acceptor =
                HeaderAcceptor::new(header, self.peer, verifier, self.active_chain.clone());
            let result = acceptor.accept();
            match result.state {
                ValidationState::Invalid => {
                    debug!(
                        "HeadersProcess accept result is invalid, error = {:?}, header = {:?}",
                        result.error, headers,
                    );
                    return StatusCode::HeadersIsInvalid
                        .with_context(format!("accept header {header:?}"));
                }
                ValidationState::TemporaryInvalid => {
                    debug!(
                        "HeadersProcess accept result is temporarily invalid, header = {:?}",
                        header
                    );
                    return Status::ok();
                }
                ValidationState::Valid => {
                    // Valid, do nothing
                }
            };
        }

        self.debug();

        if headers.len() == MAX_HEADERS_LEN {
            let start = headers.last().expect("empty checked").into();
            self.active_chain
                .send_getheaders_to_peer(self.nc, self.peer, start);
        } else if let Some(mut state) = self.synchronizer.peers().state.get_mut(&self.peer) {
            self.synchronizer
                .shared()
                .state()
                .tip_synced(state.value_mut());
        }

        // If we're in IBD, we want outbound peers that will serve us a useful
        // chain. Disconnect peers that are on chains with insufficient work.
        let peer_flags = self
            .synchronizer
            .peers()
            .get_flag(self.peer)
            .unwrap_or_default();
        if self.active_chain.is_initial_block_download()
            && headers.len() != MAX_HEADERS_LEN
            && (!peer_flags.is_protect && !peer_flags.is_whitelist && peer_flags.is_outbound)
        {
            debug!("Disconnect an unprotected outbound peer ({})", self.peer);
            let nc = Arc::clone(self.nc);
            self.synchronizer
                .shared()
                .shared()
                .async_handle()
                .spawn(async move {
                    let _ignore = nc
                        .async_disconnect(self.peer, "useless outbound peer in IBD")
                        .await;
                });
        }

        Status::ok()
    }
```

**File:** sync/src/synchronizer/headers_process.rs (L295-358)
```rust
    pub fn accept(&self) -> ValidationResult {
        let mut result = ValidationResult::default();
        let sync_shared = self.active_chain.sync_shared();
        let state = self.active_chain.state();
        let shared = sync_shared.shared();

        // FIXME If status == BLOCK_INVALID then return early. But which error
        // type should we return?
        let status = self.active_chain.get_block_status(&self.header.hash());
        if status.contains(BlockStatus::HEADER_VALID) {
            let header_index = sync_shared
                .get_header_index_view(
                    &self.header.hash(),
                    status.contains(BlockStatus::BLOCK_STORED),
                )
                .unwrap_or_else(|| {
                    panic!(
                        "header {}-{} with HEADER_VALID should exist",
                        self.header.number(),
                        self.header.hash()
                    )
                })
                .as_header_index();
            state
                .peers()
                .may_set_best_known_header(self.peer, header_index);
            return result;
        }

        if self.prev_block_check(&mut result).is_err() {
            debug!(
                "HeadersProcess rejected invalid-parent header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            return result;
        }

        if let Some(is_invalid) = self.non_contextual_check(&mut result).err() {
            debug!(
                "HeadersProcess rejected non-contextual header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            if is_invalid {
                shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            }
            return result;
        }

        if self.version_check(&mut result).is_err() {
            debug!(
                "HeadersProcess rejected invalid-version header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            return result;
        }

        sync_shared.insert_valid_header(self.peer, self.header);
        result
    }
```
