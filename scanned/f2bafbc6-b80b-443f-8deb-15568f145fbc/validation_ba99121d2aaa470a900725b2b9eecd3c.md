Audit Report

## Title
Non-IP Multiaddr Bypass in `process_listens` Allows Peer Store Pollution via Identify Protocol — (`network/src/protocols/identify/mod.rs`)

## Summary

The `process_listens` function in `IdentifyProtocol` uses `None => true` as the fallback for addresses where `multiaddr_to_socketaddr` returns `None`, unconditionally admitting non-IP multiaddrs (e.g., `/dns4/attacker.com/tcp/8115/p2p/QmXXX`) past the `global_ip_only` guard. Because `global_ip_only` is hardcoded to `true` in production and the setter is `#[cfg(test)]`-gated, any peer that completes a valid identify handshake can inject up to 10 attacker-controlled DNS addresses into the victim's peer store. These addresses are subsequently selected by the feeler path, causing the victim node to attempt outbound TCP connections to attacker-controlled hostnames, leaking the node's IP address and wasting connection resources.

## Finding Description

**Root cause — `process_listens` filter (`network/src/protocols/identify/mod.rs`, lines 139–145):**

```rust
let reachable_addrs = listens
    .into_iter()
    .filter(|addr| match multiaddr_to_socketaddr(addr) {
        Some(socket_addr) => !global_ip_only || is_reachable(socket_addr.ip()),
        None => true,   // ← non-IP addrs pass unconditionally
    })
    .collect::<Vec<_>>();
```

`multiaddr_to_socketaddr` returns `None` for `/dns4/`, `/dns6/`, and other non-IP protocol stacks. The `None => true` arm admits all of them regardless of `global_ip_only`. [1](#0-0) 

**`global_ip_only` is always `true` in production:** The constructor hardcodes `global_ip_only: true`, and the only setter is `#[cfg(test)]`-gated, making the bypass unconditional in production. [2](#0-1) 

**`add_remote_listen_addrs` writes directly to the peer store with no further address-family validation:** [3](#0-2) 

**`add_addr` performs only a ban-list check** (IP-based, ineffective against DNS addresses): [4](#0-3) 

**Injected addresses are initially connectable.** `AddrInfo::new` sets `last_connected_at_ms = 0` and `attempts_count = 0`. `is_connectable` returns `true` until `attempts_count >= ADDR_MAX_RETRIES (3)`, so injected addresses are not immediately evicted by `check_purge`. [5](#0-4) 

**Feeler path selects injected addresses.** `fetch_addrs_to_feeler` → `fetch_random` includes non-IP addresses that pass `is_connectable` and whose peer ID is not in `connected_peers`. An attacker-supplied address like `/dns4/attacker.com/tcp/8115/p2p/QmAttacker` satisfies all feeler filter conditions after the attacker disconnects. [6](#0-5) [7](#0-6) 

**Asymmetry with the local-send path** (lines 217–225) confirms the `None => true` in `process_listens` is unintentional — the outbound filter already restricts the `None` case to Onion3 only: [8](#0-7) 

## Impact Explanation

This maps to **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism.**

The peer store (`ADDR_COUNT_LIMIT = 16384`) is a critical state structure used to drive all outbound connection decisions. Injected DNS addresses:
1. Consume peer store slots until evicted after `ADDR_MAX_RETRIES = 3` failed attempts.
2. Cause the victim node to attempt outbound TCP connections to attacker-controlled hostnames, leaking the node's public IP address to the attacker via DNS resolution and TCP SYN.
3. Waste connection-attempt resources (feeler slots, TCP handshake overhead).

The peer store pollution is self-limiting (addresses are evicted after 3 failed attempts), but the IP leak and resource waste are concrete and repeatable per connection. The impact does not rise to High (no node crash, no network-wide congestion with few costs) because the store has a large capacity and the eviction mechanism eventually cleans up injected entries. [9](#0-8) 

## Likelihood Explanation

Any peer that can complete a TCP handshake and pass the identify network-name check can trigger this. No special privileges, keys, or majority hashpower are required. The attacker only needs to send a valid `IdentifyMessage` with DNS multiaddrs (including their own peer ID) in `listen_addrs`. The attack is repeatable: each new connection from the attacker injects up to 10 addresses. The `received_identify` check only validates the network name and a non-zero flag — both trivially satisfied by a legitimate-looking peer. [10](#0-9) 

## Recommendation

Replace the `None => true` fallback in `process_listens` with the same Onion3-only allowlist already used in the local-send path:

```rust
.filter(|addr| match multiaddr_to_socketaddr(addr) {
    Some(socket_addr) => !global_ip_only || is_reachable(socket_addr.ip()),
    None => addr.iter().any(|p| matches!(p, Protocol::Onion3(_))),
})
```

This aligns the inbound filter with the outbound filter at lines 217–225 and eliminates the asymmetry. [8](#0-7) 

## Proof of Concept

```rust
// 1. Construct a /dns4/ multiaddr with a peer ID — multiaddr_to_socketaddr returns None
let dns_addr: Multiaddr = "/dns4/attacker.com/tcp/8115/p2p/QmAttackerPeerID"
    .parse().unwrap();
assert!(multiaddr_to_socketaddr(&dns_addr).is_none());

// 2. Build an IdentifyMessage with this address in listen_addrs
//    (valid network name, non-zero flags — both trivially satisfied)

// 3. Send the message to a production node (global_ip_only = true)
//    process_listens hits None => true, passes dns_addr through

// 4. add_remote_listen_addrs → peer_store.add_addr(dns_addr, flags)
//    Only ban-list check; DNS address is not banned → stored with
//    last_connected_at_ms = 0, attempts_count = 0

// 5. After attacker disconnects, fetch_addrs_to_feeler selects dns_addr:
//    - extract_peer_id returns Some(QmAttackerPeerID)
//    - peer ID not in connected_peers → filter passes
//    - is_connectable returns true (attempts_count = 0 < ADDR_MAX_RETRIES = 3)

// 6. Node initiates TCP connection to attacker.com:8115,
//    revealing its public IP via DNS resolution + TCP SYN

// Verify address is in peer store:
assert!(peer_store.addr_manager().get(&base_addr(&dns_addr)).is_some());
```

A unit test can be written by constructing an `IdentifyProtocol` with a mock `Callback`, calling `process_listens` with a DNS multiaddr, and asserting the address appears in the mock peer store — mirroring the existing test structure that uses `global_ip_only(false)`.

### Citations

**File:** network/src/protocols/identify/mod.rs (L93-105)
```rust
    pub fn new(callback: T) -> IdentifyProtocol<T> {
        IdentifyProtocol {
            callback,
            remote_infos: HashMap::default(),
            global_ip_only: true,
        }
    }

    #[cfg(test)]
    pub fn global_ip_only(mut self, only: bool) -> Self {
        self.global_ip_only = only;
        self
    }
```

**File:** network/src/protocols/identify/mod.rs (L139-145)
```rust
            let reachable_addrs = listens
                .into_iter()
                .filter(|addr| match multiaddr_to_socketaddr(addr) {
                    Some(socket_addr) => !global_ip_only || is_reachable(socket_addr.ip()),
                    None => true,
                })
                .collect::<Vec<_>>();
```

**File:** network/src/protocols/identify/mod.rs (L217-225)
```rust
                .filter(|addr| {
                    if let Some(socket_addr) = multiaddr_to_socketaddr(addr) {
                        !self.global_ip_only || is_reachable(socket_addr.ip())
                    } else {
                        // allow /onion3 address
                        addr.iter()
                            .any(|protocol| matches!(protocol, Protocol::Onion3(_)))
                    }
                })
```

**File:** network/src/protocols/identify/mod.rs (L488-494)
```rust
        self.network_state.with_peer_store_mut(|peer_store| {
            for addr in addrs {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    error!("IdentifyProtocol failed to add address to peer store, address: {}, error: {:?}", addr, err);
                }
            }
        })
```

**File:** network/src/protocols/identify/mod.rs (L541-561)
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

        let flag: u64 = reader.flag().into();
        if flag == 0 {
            return None;
        }

        let raw_client_version = reader.client_version().as_utf8().ok()?.to_owned();

        Some((Flags::from_bits_truncate(flag), raw_client_version))
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L71-80)
```rust
    pub fn add_addr(&mut self, addr: Multiaddr, flags: Flags) -> Result<()> {
        if self.ban_list.is_addr_banned(&addr) {
            return Ok(());
        }
        self.check_purge()?;
        let score = self.score_config.default_score;
        self.addr_manager
            .add(AddrInfo::new(addr, 0, score, flags.bits()));
        Ok(())
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L230-239)
```rust
        let filter = |peer_addr: &AddrInfo| {
            filter(peer_addr)
                && extract_peer_id(&peer_addr.addr)
                    .map(|peer_id| !peers.contains_key(&peer_id))
                    .unwrap_or_default()
                && !peer_addr.tried_in_last_minute(now_ms)
                && !peer_addr.connected(|t| t > addr_expired_ms)
        };

        self.addr_manager.fetch_random(count, filter)
```

**File:** network/src/peer_store/types.rs (L89-105)
```rust
    pub fn is_connectable(&self, now_ms: u64) -> bool {
        // do not remove addr tried in last minute
        if self.tried_in_last_minute(now_ms) {
            return true;
        }
        // we give up if never connect to this addr
        if self.last_connected_at_ms == 0 && self.attempts_count >= ADDR_MAX_RETRIES {
            return false;
        }
        // consider addr is not connectable if failed too many times
        if now_ms.saturating_sub(self.last_connected_at_ms) > ADDR_TIMEOUT_MS
            && (self.attempts_count >= ADDR_MAX_FAILURES)
        {
            return false;
        }
        true
    }
```

**File:** network/src/peer_store/addr_manager.rs (L74-90)
```rust
                None => {
                    if filter(&addr_info) {
                        if addr_info.is_connectable(now_ms)
                            || addr_info
                                .addr
                                .iter()
                                .any(|p| matches!(p, Protocol::Onion3(_)))
                        {
                            addr_infos.push(addr_info);
                        } else {
                            debug!(
                                "addr {:?} is not connectable and not an onion address",
                                addr_info.addr
                            );
                        }
                    }
                }
```

**File:** network/src/peer_store/mod.rs (L26-35)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
/// Consider we never seen a peer if peer's last_connected_at beyond this timeout
const ADDR_TIMEOUT_MS: u64 = 7 * 24 * 3600 * 1000;
/// The timeout that peer's address should be added to the feeler list again
pub(crate) const ADDR_TRY_TIMEOUT_MS: u64 = 3 * 24 * 3600 * 1000;
/// When obtaining the list of selectable nodes for identify,
/// the node that has just been disconnected needs to be excluded
pub(crate) const DIAL_INTERVAL: u64 = 15 * 1000;
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
```
