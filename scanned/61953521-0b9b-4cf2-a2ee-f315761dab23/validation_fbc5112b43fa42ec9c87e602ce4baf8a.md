The code path is fully traceable. Here is the analysis:

---

### Title
Unauthenticated Anchor List Poisoning via BlockRelayOnly Promotion in `accept_peer` — (`network/src/peer_registry.rs`)

### Summary

`accept_peer()` unconditionally calls `peer_store.add_anchors(remote_addr)` for any outbound connection that triggers the BlockRelayOnly promotion path, before any protocol-level validation. This allows an attacker whose address is in the victim's peer store to have their address persisted to `anchors.db` on shutdown and dialed with priority on the next restart.

### Finding Description

In `accept_peer()`, when `non_whitelist_outbound >= max_outbound` and `block_relay_only_outbound_count < MAX_OUTBOUND_BLOCK_RELAY` (default 2), the code at lines 129–131 executes:

```rust
peer_store.add_anchors(remote_addr.clone());
session_type = SessionType::BlockRelayOnly;
``` [1](#0-0) 

`add_anchors` is a bare insert with no validation:

```rust
pub fn add_anchors(&mut self, addr: Multiaddr) {
    self.anchors.add(addr);
}
``` [2](#0-1) 

The only guard before this point is a ban-list check (line 109). There is no score check, no identify-protocol validation, and no proof that the peer is a legitimate CKB node.

On shutdown, `DumpPeerStoreService::drop()` calls `dump_peer_store()` → `dump_to_dir()` → `self.anchors().dump(...)`, which serializes up to `MAX_OUTBOUND_BLOCK_RELAY=2` anchor addresses to `anchors.db`: [3](#0-2) [4](#0-3) 

On the next startup, `NetworkService::start()` drains the anchor list and dials those addresses **before** bootnodes and before any other peer store addresses:

```rust
let anchors: Vec<_> = peer_store.mut_anchors().drain().collect();
addrs.extend(anchors);
``` [5](#0-4) 

**Attack path:**

1. Attacker propagates their address to the victim via standard addr gossip (any connected peer can do this).
2. Victim fills its regular outbound slots (`non_whitelist_outbound >= max_outbound = 8` by default).
3. Victim's `OutboundPeerService` continues dialing from the peer store; it dials the attacker's address.
4. `accept_peer()` fires with `raw_session_type = Outbound`, hits the BlockRelayOnly branch, and calls `add_anchors(attacker_addr)` immediately.
5. On graceful shutdown, attacker's address is written to `anchors.db`.
6. On restart, victim dials attacker first, before any legitimate peer.

The `connection_status()` counter correctly distinguishes `Outbound` from `BlockRelayOnly` (they are separate enum variants), so the precondition `block_relay_only_outbound_count < 2` is the normal state for a node that has just filled its regular outbound slots: [6](#0-5) [7](#0-6) 

`disable_block_relay_only_connection` defaults to `false` in production config, so the vulnerable branch is active by default: [8](#0-7) 

### Impact Explanation

An attacker who can get their address into the victim's peer store (trivially achievable via addr gossip) can fill both anchor slots with attacker-controlled addresses. On every subsequent restart, the victim's first two outbound connections go to the attacker. This is the prerequisite for an eclipse attack: the attacker can selectively withhold or delay blocks, enabling consensus deviation or double-spend facilitation against the victim node.

### Likelihood Explanation

- Getting an address into a peer store via addr gossip requires no privilege — any peer connected to the victim (or any peer connected to a peer of the victim) can do it.
- The precondition (outbound full, block-relay-only not full) is the normal operating state of a node that has recently started and filled its regular outbound slots.
- The anchor list holds only 2 entries, so a single attacker controlling 2 addresses can fully saturate it.
- The `DumpPeerStoreService` also periodically dumps every hour, so the poisoned state persists even without a clean shutdown. [9](#0-8) 

### Recommendation

`add_anchors` should only be called for peers that have completed the identify protocol handshake and have been validated as legitimate CKB nodes. The anchor insertion should be deferred to the point where the peer's `identify_info` is confirmed (e.g., in `IdentifyCallback::received_identify` after `required_flags_filter` passes), not at raw TCP connection acceptance time.

Additionally, anchors should be evicted when a peer is banned or scores below the ban threshold.

### Proof of Concept

State test (no network required):

```rust
// 1. Create registry with max_outbound=1, disable_block_relay_only=false
let mut peer_store = PeerStore::default();
let mut registry = PeerRegistry::new(10, 1, false, vec![], false);

// 2. Fill the single outbound slot with a legitimate peer
registry.accept_peer(legit_addr, 1.into(), RawSessionType::Outbound, &mut peer_store).unwrap();

// 3. Dial attacker address (outbound, slots full, block_relay_only_count=0 < 2)
registry.accept_peer(attacker_addr.clone(), 2.into(), RawSessionType::Outbound, &mut peer_store).unwrap();

// 4. Assert attacker address is now in anchors
assert!(peer_store.anchors().contains(&attacker_addr));

// 5. Simulate shutdown dump + restart load
let dir = tempfile::tempdir().unwrap();
peer_store.dump_to_dir(dir.path()).unwrap();
let mut peer_store2 = PeerStore::load_from_dir_or_default(dir.path());

// 6. Assert anchors.drain() returns attacker address (dialed first on restart)
let drained: Vec<_> = peer_store2.mut_anchors().drain().collect();
assert!(drained.contains(&attacker_addr));
``` [10](#0-9)

### Citations

**File:** network/src/peer_registry.rs (L123-133)
```rust
            } else if connection_status.non_whitelist_outbound >= self.max_outbound {
                if self.disable_block_relay_only_connection
                    || connection_status.block_relay_only_outbound_count
                        >= self.max_outbound_block_relay
                {
                    return Err(PeerError::ReachMaxOutboundLimit.into());
                } else {
                    peer_store.add_anchors(remote_addr.clone());
                    session_type = SessionType::BlockRelayOnly;
                }
            }
```

**File:** network/src/peer_registry.rs (L294-307)
```rust
    pub(crate) fn connection_status(&self) -> ConnectionStatus {
        let total = self.peers.len() as u32;
        let mut non_whitelist_inbound: u32 = 0;
        let mut non_whitelist_outbound: u32 = 0;
        let mut block_relay_only_outbound_count: u32 = 0;
        for peer in self.peers.values().filter(|peer| !peer.is_whitelist) {
            if peer.is_outbound() {
                non_whitelist_outbound += 1;
            } else if peer.is_block_relay_only() {
                block_relay_only_outbound_count += 1;
            } else {
                non_whitelist_inbound += 1;
            }
        }
```

**File:** network/src/peer_store/peer_store_impl.rs (L116-119)
```rust
    /// Add anchors address
    pub fn add_anchors(&mut self, addr: Multiaddr) {
        self.anchors.add(addr);
    }
```

**File:** network/src/peer_store/peer_store_db.rs (L92-101)
```rust
    pub fn dump(&self, mut file: File) -> Result<(), Error> {
        let addrs: Vec<_> = self.dump_iter().collect();
        debug!("Anchors dump {} addrs", addrs.len());
        // empty file and dump the json string to it
        file.set_len(0)
            .and_then(|_| serde_json::to_string(&addrs).map_err(Into::into))
            .and_then(|json_string| file.write_all(json_string.as_bytes()))
            .and_then(|_| file.sync_all())
            .map_err(Into::into)
    }
```

**File:** network/src/services/dump_peer_store.rs (L11-11)
```rust
const DEFAULT_DUMP_INTERVAL: Duration = Duration::from_secs(3600); // 1 hour
```

**File:** network/src/services/dump_peer_store.rs (L49-61)
```rust
impl Drop for DumpPeerStoreService {
    fn drop(&mut self) {
        debug!("Dump peer store before exiting");
        self.dump_peer_store();
        #[cfg(target_family = "wasm")]
        {
            use crate::peer_store::browser::get_db;
            let path = self.network_state.config.peer_store_path();
            p2p::runtime::spawn(async {
                let _ignore = get_db(path).await.shutdown().await;
            });
        }
    }
```

**File:** network/src/network.rs (L1186-1205)
```rust
            // tried to re-connect to anchors on startup
            let anchors: Vec<_> = peer_store.mut_anchors().drain().collect();
            addrs.extend(anchors);
            // Get bootnodes randomly
            let bootnodes = self
                .network_state
                .bootnodes
                .iter()
                .choose_multiple(&mut rand::thread_rng(), count.saturating_sub(addrs.len()))
                .into_iter()
                .cloned();
            addrs.extend(bootnodes);
            addrs
        });

        // dial half bootnodes
        for addr in bootnodes {
            debug!("Dial bootnode {:?}", addr);
            self.network_state.dial_identify(&p2p_control, addr);
        }
```

**File:** network/src/peer.rs (L44-58)
```rust
impl SessionType {
    /// is outbound
    pub fn is_outbound(&self) -> bool {
        matches!(self, SessionType::Outbound)
    }

    /// is inbound
    pub fn is_inbound(&self) -> bool {
        matches!(self, SessionType::Inbound)
    }

    /// is block_relay_only
    pub fn is_block_relay_only(&self) -> bool {
        matches!(self, SessionType::BlockRelayOnly)
    }
```

**File:** util/app-config/src/configs/network.rs (L92-94)
```rust
    /// Disable block_relay_only connection, only use for testing.
    #[serde(default)]
    pub disable_block_relay_only_connection: bool,
```

**File:** network/src/peer_store/anchors.rs (L27-34)
```rust
    pub fn dump_iter(&self) -> impl Iterator<Item = &Multiaddr> {
        self.addrs.iter().take(MAX_OUTBOUND_BLOCK_RELAY as usize)
    }

    /// Anchors drain
    pub fn drain(&mut self) -> impl Iterator<Item = Multiaddr> {
        self.addrs.drain()
    }
```
