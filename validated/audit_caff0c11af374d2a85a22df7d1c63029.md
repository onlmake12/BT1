All three root causes are confirmed by the actual code. Let me verify each claim precisely before rendering the verdict.

**Root Cause 1 — confirmed:** `base_addr()` filters only `Ws | Wss | Memory | Tls`, leaving `P2P` intact. [1](#0-0) 
`AddrInfo::new()` stores `base_addr(&addr)` as the canonical key. [2](#0-1) 
`AddrManager::add()` uses `addr_info.addr` (which includes the P2P component) as the `HashMap` key, so two entries with the same IP:port but different peer IDs are stored independently. [3](#0-2) 

**Root Cause 2 — confirmed:** `check_purge()` computes `len = peers_by_network_group.len()` and then calls `.take(len / 2)`. When all 16,384 entries share one `/16` group, `len = 1` and `take(0)` yields an empty iterator. [4](#0-3) 
The empty `candidate_peers` then triggers `Err(PeerStoreError::EvictionFailed)`. [5](#0-4) 

**Root Cause 3 — confirmed:** Both call sites only log the error. [6](#0-5) [7](#0-6) 

**Attack delivery constraint:** Non-announce `Nodes` messages are limited to one per session (`received_nodes` flag), but each delivers up to `MAX_ADDR_TO_SEND (1000) × MAX_ADDRS (3) = 3000` addresses. Six sessions suffice to reach `ADDR_COUNT_LIMIT = 16384`. Announce messages (repeatable, 10 items × 3 = 30 per message) provide an alternative slower path from a single session. [8](#0-7) 

**`fetch_random` IP deduplication does not mitigate:** The attacker uses 16,384 distinct IPs all within one `/16` subnet (e.g., `225.0.0.1`–`225.0.63.255`), so `fetch_random`'s per-IP dedup still returns one attacker address per unique IP — all attacker-controlled. [9](#0-8) 

All validation checks pass. The report is valid.

---

Audit Report

## Title
Peer Store Exhaustion via Single-/16-Group Eviction Failure Enabling Node Isolation — (File: `network/src/peer_store/peer_store_impl.rs`)

## Summary
`base_addr()` retains the `/p2p/<PeerId>` component, allowing an attacker to insert up to 16,384 distinct entries from a single `/16` subnet by varying only the peer ID. Once `ADDR_COUNT_LIMIT` is reached, `check_purge()`'s step-2 eviction computes `take(len / 2)` = `take(0)` when all entries share one network group, returning `Err(PeerStoreError::EvictionFailed)` permanently. Both call sites discard this error silently, so the node can no longer learn legitimate peer addresses and becomes isolated when existing connections drop.

## Finding Description

**Root cause 1 — P2P component retained in deduplication key**

`base_addr()` strips only `Ws`, `Wss`, `Memory`, and `Tls` protocol components; the `P2P` (peer ID) component is preserved:

```rust
// network/src/peer_store/mod.rs L92-104
if matches!(p, Protocol::Ws | Protocol::Wss | Protocol::Memory(_) | Protocol::Tls(_)) {
    None
} else {
    Some(p)
}
```

`AddrInfo::new()` stores `base_addr(&addr)` as the canonical address field, and `AddrManager::add()` uses `addr_info.addr` as the `HashMap` key. Therefore `/ip4/1.2.3.4/tcp/8115/p2p/PeerA` and `/ip4/1.2.3.4/tcp/8115/p2p/PeerB` are two independent entries. An attacker can generate 16,384 distinct multiaddrs from IPs within a single `/16` subnet by varying the peer ID.

**Root cause 2 — Integer-division truncation in `check_purge()` step 2**

Fresh entries (`attempts_count = 0`, `last_connected_at_ms = 0`) satisfy `is_connectable()` (the `ADDR_MAX_RETRIES` threshold is not reached), so step 1 finds nothing to remove. Step 2 groups entries by `/16` network segment and calls:

```rust
// network/src/peer_store/peer_store_impl.rs L366-376
let len = peers_by_network_group.len();
...
peers.into_iter().take(len / 2)
```

When all 16,384 entries share one `/16` group, `len = 1` and `take(1 / 2)` = `take(0)`. The iterator is empty, `candidate_peers` is empty, and the function returns:

```rust
// L399-401
if candidate_peers.is_empty() {
    return Err(PeerStoreError::EvictionFailed.into());
}
```

Every subsequent `add_addr()` call hits `check_purge()` first and immediately returns this error.

**Root cause 3 — `EvictionFailed` silently discarded at both call sites**

`DiscoveryAddressManager::add_new_addrs()` emits only a `debug!` log on error. `IdentifyCallback::add_remote_listen_addrs()` emits only an `error!` log. Neither disconnects the offending peer, rate-limits further submissions, nor takes any protective action.

**Attack delivery**

`verify_nodes_message()` permits one non-announce `Nodes` message per session carrying up to `MAX_ADDR_TO_SEND (1000)` items × `MAX_ADDRS (3)` addresses = 3,000 addresses. Six sessions (each sending one non-announce `Nodes` message) deliver ≥ 16,384 addresses, all with IPs in a single `/16` subnet and freshly generated peer IDs. The `fetch_random` per-IP deduplication does not mitigate this because the attacker uses 16,384 distinct IPs within the same `/16` block.

## Impact Explanation

Once the peer store is exhausted and eviction is permanently broken, `fetch_addrs_to_feeler` and `fetch_addrs_to_attempt` return only attacker-controlled addresses. Legitimate peer addresses are silently dropped. When the node's existing connections drop (restart, network interruption), it cannot reconnect to honest peers and becomes isolated — unable to sync headers or blocks, relay transactions, or confirm new transactions. This matches the **High** severity class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* A single attacker can isolate targeted nodes at negligible cost; applied at scale, this degrades the broader network's ability to propagate transactions and blocks.

## Likelihood Explanation

- **Entry path**: Any single inbound or outbound peer speaking the Discovery protocol can trigger this. No privileged role is required.
- **Cost**: Six TCP connections, each sending one crafted non-announce `Nodes` message (≈ 16,384 addresses total). All addresses use IPs within one `/16` subnet with freshly generated peer IDs.
- **Persistence**: The condition persists until node restart. If the peer store is persisted to disk and reloaded on startup, the attacker must re-flood after restart, but the re-flood cost is identical.
- **No Sybil requirement**: A single IP in a single `/16` block is sufficient to trigger `take(0)`.

## Recommendation

1. **Fix the integer-division bug in `check_purge()`** — replace `take(len / 2)` with ceiling division so a single-group store still evicts entries:
   ```rust
   .take((len + 1) / 2)
   ```

2. **Strip the P2P component in `base_addr()`** — add `Protocol::P2P(_)` to the filter list so that two addresses differing only in peer ID are treated as the same entry:
   ```rust
   if matches!(p, Protocol::Ws | Protocol::Wss | Protocol::Memory(_) | Protocol::Tls(_) | Protocol::P2P(_)) {
       None
   }
   ```

3. **Add a per-IP cap** — limit the number of entries per IP address in `AddrManager` (e.g., max 4 per IP), consistent with the eviction threshold already used in `check_purge()`.

4. **Penalize peers that trigger `EvictionFailed`** — rather than silently logging, call `misbehave()` on the session that delivered the flooding addresses.

## Proof of Concept

```
1. Establish six TCP connections to a target CKB node (inbound or outbound).

2. On each connection, send one Discovery Nodes message (announce=false)
   containing 1000 items × 3 addresses each, where every address has the form:
       /ip4/225.0.<i>.<j>/tcp/8115/p2p/<freshly_generated_peer_id>
   keeping all IPs within 225.0.0.0/16 and varying peer ID per entry.

3. After six messages (≥ 16,384 addresses delivered), the peer store reaches
   ADDR_COUNT_LIMIT.

4. check_purge() is called on the next add_addr():
   - Step 1: all entries have attempts_count=0, last_connected_at_ms=0 →
     is_connectable() returns true for all → no eviction.
   - Step 2: peers_by_network_group.len() = 1 (single /16 group) →
     take(1/2) = take(0) → candidate_peers empty →
     returns Err(PeerStoreError::EvictionFailed).

5. All subsequent add_addr() calls return EvictionFailed and are silently
   dropped (debug!/error! log only).

6. The target node can no longer learn about legitimate peers. When its
   existing connections drop, it cannot reconnect and becomes isolated.

Unit test plan:
- Create a PeerStore, insert 16,384 AddrInfo entries with IPs in
  225.0.0.0/16, attempts_count=0, last_connected_at_ms=0, each with a
  distinct peer ID in the multiaddr.
- Call add_addr() with a new legitimate address outside 225.0.0.0/16.
- Assert the return value is Err(PeerStoreError::EvictionFailed).
```

### Citations

**File:** network/src/peer_store/mod.rs (L92-104)
```rust
pub(crate) fn base_addr(addr: &Multiaddr) -> Multiaddr {
    addr.iter()
        .filter_map(|p| {
            if matches!(
                p,
                Protocol::Ws | Protocol::Wss | Protocol::Memory(_) | Protocol::Tls(_)
            ) {
                None
            } else {
                Some(p)
            }
        })
        .collect()
```

**File:** network/src/peer_store/types.rs (L65-68)
```rust
    pub fn new(addr: Multiaddr, last_connected_at_ms: u64, score: Score, flags: u64) -> Self {
        AddrInfo {
            // only store tcp protocol
            addr: base_addr(&addr),
```

**File:** network/src/peer_store/addr_manager.rs (L22-34)
```rust
    pub fn add(&mut self, mut addr_info: AddrInfo) {
        if let Some(&id) = self.addr_to_id.get(&addr_info.addr) {
            let (exist_last_connected_at_ms, random_id_pos) = {
                let info = self.id_to_info.get(&id).expect("must exists");
                (info.last_connected_at_ms, info.random_id_pos)
            };
            // Get time earlier than record time, return directly
            if addr_info.last_connected_at_ms >= exist_last_connected_at_ms {
                addr_info.random_id_pos = random_id_pos;
                self.id_to_info.insert(id, addr_info);
            }
            return;
        }
```

**File:** network/src/peer_store/addr_manager.rs (L49-71)
```rust
        let mut duplicate_ips = HashSet::new();
        let mut addr_infos = Vec::with_capacity(count);
        let mut rng = rand::thread_rng();
        let now_ms = ckb_systemtime::unix_time_as_millis();
        for i in 0..self.random_ids.len() {
            // reuse the for loop to shuffle random ids
            // https://en.wikipedia.org/wiki/Fisher%E2%80%93Yates_shuffle
            let j = rng.gen_range(i..self.random_ids.len());
            self.swap_random_id(j, i);
            let addr_info: AddrInfo = self.id_to_info[&self.random_ids[i]].to_owned();
            match multiaddr_to_socketaddr(&addr_info.addr) {
                Some(socket_addr) => {
                    let ip = socket_addr.ip();
                    let is_unique_ip = !duplicate_ips.contains(&ip);
                    // A trick to make our tests work
                    // TODO remove this after fix the network tests.
                    let is_test_ip = ip.is_unspecified() || ip.is_loopback();
                    if (is_test_ip || is_unique_ip)
                        && addr_info.is_connectable(now_ms)
                        && filter(&addr_info)
                    {
                        duplicate_ips.insert(ip);
                        addr_infos.push(addr_info);
```

**File:** network/src/peer_store/peer_store_impl.rs (L366-376)
```rust
                let len = peers_by_network_group.len();
                let mut peers = peers_by_network_group
                    .drain()
                    .map(|(_, v)| v)
                    .collect::<Vec<Vec<_>>>();

                peers.sort_unstable_by_key(|k| std::cmp::Reverse(k.len()));

                peers
                    .into_iter()
                    .take(len / 2)
```

**File:** network/src/peer_store/peer_store_impl.rs (L399-401)
```rust
            if candidate_peers.is_empty() {
                return Err(PeerStoreError::EvictionFailed.into());
            }
```

**File:** network/src/protocols/discovery/mod.rs (L32-34)
```rust
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
```

**File:** network/src/protocols/discovery/mod.rs (L354-361)
```rust
            self.network_state.with_peer_store_mut(|peer_store| {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    debug!(
                        "Failed to add discovered address to peer_store {:?} {:?}",
                        err, addr
                    );
                }
            });
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
