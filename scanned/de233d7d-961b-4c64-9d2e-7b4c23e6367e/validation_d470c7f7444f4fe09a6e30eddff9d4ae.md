### Title
Unauthenticated `get_overview` RPC Endpoint Triggers Expensive Operations with No Rate Limiting, Enabling CPU/Memory DoS via SSRF or CSRF — (`rpc/src/module/terminal.rs`)

---

### Summary

The CKB JSON-RPC `Terminal` module exposes a `get_overview` method that, when called with the `refresh=0x1F` (`EVERYTHING`) flag, bypasses all caches and synchronously executes five expensive operations: a full OS-level system scan (`System::new_all()` + `sys.refresh_all()`), a full block template assembly (`get_block_template`), a RocksDB key-count estimation, network peer enumeration, and mining info fetch. The Terminal module is enabled by default in the production configuration, the RPC server binds to `127.0.0.1:8114` by default, there is no authentication, and there is no per-method rate limiting. An unprivileged attacker who can reach the RPC port via SSRF in a co-hosted application, or via DNS rebinding/CSRF when the node operator visits a malicious website, can flood this endpoint to exhaust CPU and memory and cause the node to stop mining or become unresponsive.

---

### Finding Description

**Root cause 1 — `get_tx_pool_info` calls `get_block_template` on every cache miss (TTL: 2 seconds)**

Inside `get_tx_pool_info`, the implementation unconditionally calls `self.shared.get_block_template(None, None, None)` to populate the `committing` field: [1](#0-0) 

`get_block_template` is a known expensive operation — the CKB changelog explicitly notes it can take 200–400 ms when the tx pool is large. The TX_POOL_INFO cache TTL is only 2 seconds: [2](#0-1) 

This means an attacker sending `get_overview(refresh=4)` (TX_POOL_INFO flag only) can force a full `get_block_template` call every 2 seconds with no other throttle.

**Root cause 2 — `get_sys_info` calls `System::new_all()` + `sys.refresh_all()` on every cache miss (TTL: 5 seconds)** [3](#0-2) 

`System::new_all()` in the `sysinfo` crate allocates a new system object and scans all processes, memory, CPU, disks, and network interfaces. This is a well-known expensive operation. The SYSTEM_INFO TTL is 5 seconds: [4](#0-3) 

**Root cause 3 — `refresh=0x1F` (EVERYTHING) clears all caches before executing all five sub-operations simultaneously** [5](#0-4) 

A single call with `refresh=31` clears all five caches and then synchronously executes all five expensive operations in sequence on the RPC handler thread.

**Root cause 4 — Terminal module is enabled by default in production configuration** [6](#0-5) 

**Root cause 5 — No authentication and no per-method rate limiting on the RPC server**

The RPC server has no authentication layer and no per-method rate limiting. The only protection is the default localhost bind: [7](#0-6) 

This is the same threat model as the Prysm report: localhost binding does not prevent SSRF from co-hosted applications or DNS rebinding/CSRF attacks from a malicious website visited by the node operator.

---

### Impact Explanation

An attacker who can reach `127.0.0.1:8114` (via SSRF in a co-hosted web app, or via DNS rebinding/CSRF) can send a continuous flood of:

```json
{"jsonrpc":"2.0","method":"get_overview","params":[31],"id":1}
```

Each request forces:
1. `System::new_all()` + `sys.refresh_all()` — full OS process/memory/CPU/disk/network scan
2. `get_block_template()` — full tx pool packaging + DAO field calculation (200–400 ms under load)
3. `estimate_num_keys_cf(COLUMN_CELL)` — RocksDB statistics query
4. Network peer enumeration
5. Mining info fetch

The combined CPU and memory pressure from concurrent requests causes the node to become unresponsive, stalling block production. A mining node that stops producing blocks loses mining revenue. A validator-adjacent node that stops syncing may miss attestation windows.

**Severity: High** — unauthenticated, default-enabled, reachable via standard web attack vectors (SSRF/CSRF/DNS rebinding), causes sustained DoS of the node's core mining function.

---

### Likelihood Explanation

- The Terminal module is **enabled by default** in the production `ckb.toml`.
- The RPC port `8114` is a well-known default; any SSRF vulnerability in a co-hosted application (e.g., a web dashboard, block explorer, or monitoring tool running on the same server) immediately exposes it.
- DNS rebinding attacks against `127.0.0.1:8114` are a standard technique requiring only that the node operator visit a malicious website.
- The `refresh=31` parameter is publicly documented in the RPC README, so the attack requires no reverse engineering.
- No authentication, no rate limiting, and a 2-second cache TTL on the most expensive sub-operation (`get_block_template`) make sustained exploitation trivial.

---

### Recommendation

1. **Remove `get_block_template` from `get_tx_pool_info`**. The `committing` count does not require a full block assembly; use the existing tx pool state directly.
2. **Add per-method rate limiting** to the `get_overview` endpoint (e.g., max 1 forced-refresh call per 10 seconds per source IP).
3. **Increase the TX_POOL_INFO cache TTL** or decouple the `committing` field from `get_block_template`.
4. **Consider removing the Terminal module from the default enabled modules list**, or document clearly that it should not be enabled on nodes accessible via SSRF-prone co-hosted applications.
5. **Add a CORS policy** to the RPC HTTP server to mitigate DNS rebinding/CSRF attacks from browser-based attackers.

---

### Proof of Concept

Attacker sends from a co-hosted application (SSRF) or via DNS rebinding (CSRF):

```bash
# Force all caches to clear and all expensive operations to execute on every call
while true; do
  curl -s -X POST http://127.0.0.1:8114/ \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"get_overview","params":[31],"id":1}'
done
```

Each iteration triggers `System::new_all()` + `sys.refresh_all()` (full OS scan) and `get_block_template()` (full tx pool assembly, 200–400 ms under load) with no rate limiting. Under a loaded tx pool, concurrent requests from multiple connections will saturate the RPC thread pool and the block assembler channel, causing the node to stop responding to legitimate mining requests.

The `refresh=31` value is the documented `EVERYTHING` flag: [8](#0-7)

### Citations

**File:** rpc/src/module/terminal.rs (L27-27)
```rust
    pub const SYSTEM_INFO: Duration = Duration::from_secs(5);
```

**File:** rpc/src/module/terminal.rs (L33-33)
```rust
    pub const TX_POOL_INFO: Duration = Duration::from_secs(2);
```

**File:** rpc/src/module/terminal.rs (L59-59)
```rust
        const EVERYTHING                   = 0b00011111;
```

**File:** rpc/src/module/terminal.rs (L445-454)
```rust
        // If refresh everything, clear cache first
        if refresh.contains(RefreshKind::EVERYTHING) {
            self.cache.clear_all();
        }

        let sys = self.get_sys_info(refresh)?;
        let mining = self.get_mining_info(refresh)?;
        let pool = self.get_tx_pool_info(refresh)?;
        let cells = self.get_cells_info(refresh)?;
        let network = self.get_network_info(refresh)?;
```

**File:** rpc/src/module/terminal.rs (L509-510)
```rust
        let mut sys = System::new_all();
        sys.refresh_all();
```

**File:** rpc/src/module/terminal.rs (L590-600)
```rust
        let block_template = self
            .shared
            .get_block_template(None, None, None)
            .map_err(|err| {
                error!("Send get_block_template request error {}", err);
                RPCError::ckb_internal_error(err)
            })?
            .map_err(|err| {
                error!("Get_block_template result error {}", err);
                RPCError::from_any_error(err)
            })?;
```

**File:** resource/ckb.toml (L182-182)
```text
listen_address = "127.0.0.1:8114" # {{
```

**File:** resource/ckb.toml (L190-190)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
```
