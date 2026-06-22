### Title
Unauthenticated Cache Bypass via `get_overview` Forcing Repeated Expensive System-Level Recomputation — (File: `rpc/src/module/terminal.rs`)

---

### Summary

The `get_overview` RPC method in the Terminal module accepts a caller-controlled `refresh` bit-flag parameter. When an unprivileged caller passes `0x1F` (`EVERYTHING`), the handler unconditionally clears all cached data and re-executes every expensive sub-operation — including a full OS system scan (`System::new_all()` + `sys.refresh_all()`), a block template assembly (`get_block_template`), a RocksDB key-count estimation, and a full peer enumeration — on every call, with no authentication, no rate limiting, and no per-caller isolation. This directly mirrors the reported class: unauthenticated cache purging leading to resource exhaustion and potential denial of service.

---

### Finding Description

The `TerminalRpc::get_overview` handler is defined at: [1](#0-0) 

The `refresh` parameter is a raw `u32` accepted from the caller with no authentication or authorization check. When the caller supplies `refresh = 0x1F` (`RefreshKind::EVERYTHING`), the first action is: [2](#0-1) 

This calls `clear_all()`, which wipes all five in-process caches: [3](#0-2) 

After clearing, the handler unconditionally re-fetches all data fresh. The most expensive path is `get_sys_info`, which allocates a new `System` object and calls `refresh_all()` — a blocking OS-level syscall that enumerates all processes, memory, disks, and network interfaces: [4](#0-3) 

The `get_tx_pool_info` path is equally expensive: it calls `get_block_template(None, None, None)` — a full block assembly operation over the entire tx-pool — on every forced refresh: [5](#0-4) 

The `get_cells_info` path calls `estimate_num_keys_cf(COLUMN_CELL)` — a RocksDB column scan: [6](#0-5) 

The Terminal module is enabled by default in the shipped configuration: [7](#0-6) 

The `get_overview` trait method signature accepts any `Option<u32>` with no access control wrapper: [8](#0-7) 

There is no authentication, no per-IP rate limiting, and no per-caller isolation anywhere in the handler or its sub-functions.

---

### Impact Explanation

An attacker who can reach the RPC port (default `127.0.0.1:8114`, but frequently exposed publicly by node operators) can send a tight loop of:

```json
{"jsonrpc":"2.0","method":"get_overview","params":[31],"id":1}
```

Each call:
1. Clears all five caches, defeating the TTL-based protection for all concurrent legitimate callers.
2. Forces `System::new_all()` + `sys.refresh_all()` — a blocking, CPU- and memory-intensive OS enumeration.
3. Forces `get_block_template` — a full tx-pool assembly that contends with the miner's own block template requests.
4. Forces a RocksDB column scan (`estimate_num_keys_cf`).

The cache is shared across all callers. A single attacker continuously invalidating it means legitimate TUI monitoring tools always pay the full re-computation cost. More critically, the forced `get_block_template` calls contend with the miner's block assembly pipeline, potentially delaying block production. Sustained at high frequency, this constitutes a CPU/IO denial-of-service against the node process.

**Impact: High** — resource exhaustion, miner block-assembly interference, and effective DoS of the Terminal RPC module for all legitimate users.

---

### Likelihood Explanation

The attack requires only an HTTP POST to the RPC port with a crafted JSON body — no credentials, no keys, no protocol knowledge beyond the public RPC documentation. The Terminal module is enabled by default. Many operators expose the RPC port publicly (the configuration warns against it but does not enforce localhost-only). The attack is trivially scriptable with `curl` or any HTTP client. A single attacker with network access to the RPC port can sustain the attack indefinitely.

**Likelihood: High** — zero-privilege, single HTTP request, publicly documented parameter, default-enabled module.

---

### Recommendation

1. **Require authentication** for the `refresh` parameter values that trigger cache invalidation (e.g., any non-zero `refresh` flag), or restrict the Terminal module to authenticated callers only.
2. **Rate-limit forced refreshes** per caller IP or globally (e.g., at most one full refresh per TTL window).
3. **Decouple `get_block_template` from `get_overview`**: the block template assembly is the most expensive sub-call and should not be triggered by an unauthenticated cache-bypass path.
4. **Do not clear the cache before re-fetching**: the `clear_all()` call at line 447 is unnecessary — the subsequent per-subsystem refresh flags already bypass the cache check. Removing it eliminates the window where concurrent legitimate callers are forced to re-fetch.

---

### Proof of Concept

```bash
# Attacker with access to the RPC port runs in a tight loop:
while true; do
  curl -s -X POST http://<node-rpc-host>:8114/ \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"get_overview","params":[31],"id":1}'
done
```

Each iteration:
- Clears all five `TerminalCache` entries (line 447), invalidating cached data for all concurrent callers.
- Triggers `System::new_all()` + `sys.refresh_all()` (lines 509–510), a blocking OS-level process/memory/disk/network enumeration.
- Triggers `get_block_template(None, None, None)` (line 592), a full tx-pool block assembly.
- Triggers `estimate_num_keys_cf(COLUMN_CELL)` (line 639), a RocksDB column scan.

The node's CPU and I/O spike continuously. The miner's `get_block_template` pipeline is starved. Legitimate monitoring clients always receive stale-then-recomputed data at full cost. No credential or privilege is required.

### Citations

**File:** rpc/src/module/terminal.rs (L239-245)
```rust
    pub fn clear_all(&self) {
        self.sys_info.lock().clear();
        self.mining_info.lock().clear();
        self.tx_pool_info.lock().clear();
        self.cells_info.lock().clear();
        self.network_info.lock().clear();
    }
```

**File:** rpc/src/module/terminal.rs (L427-428)
```rust
    #[rpc(name = "get_overview")]
    fn get_overview(&self, refresh: Option<u32>) -> Result<Overview>;
```

**File:** rpc/src/module/terminal.rs (L440-464)
```rust
    fn get_overview(&self, refresh: Option<u32>) -> Result<Overview> {
        let refresh = refresh
            .and_then(RefreshKind::from_bits)
            .unwrap_or(RefreshKind::NOTHING);

        // If refresh everything, clear cache first
        if refresh.contains(RefreshKind::EVERYTHING) {
            self.cache.clear_all();
        }

        let sys = self.get_sys_info(refresh)?;
        let mining = self.get_mining_info(refresh)?;
        let pool = self.get_tx_pool_info(refresh)?;
        let cells = self.get_cells_info(refresh)?;
        let network = self.get_network_info(refresh)?;

        Ok(Overview {
            sys,
            cells,
            mining,
            pool,
            network,
            version: self.network_controller.version().to_owned(),
        })
    }
```

**File:** rpc/src/module/terminal.rs (L509-534)
```rust
        let mut sys = System::new_all();
        sys.refresh_all();

        let total_memory = sys.total_memory();
        let used_memory = sys.used_memory();
        let global_cpu_usage = sys.global_cpu_usage();
        let sys_disks = SysDisks::new_with_refreshed_list();
        let disks = sys_disks
            .iter()
            .map(|disk| Disk {
                total_space: disk.total_space(),
                available_space: disk.available_space(),
                is_removable: disk.is_removable(),
            })
            .collect();
        let sys_networks = SysNetworks::new_with_refreshed_list();
        let networks = sys_networks
            .iter()
            .map(|(name, data)| Network {
                interface_name: name.clone(),
                received: data.received(),
                total_received: data.total_received(),
                transmitted: data.transmitted(),
                total_transmitted: data.total_transmitted(),
            })
            .collect();
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

**File:** rpc/src/module/terminal.rs (L636-643)
```rust
        let estimate_live_cells_num = self
            .shared
            .store()
            .estimate_num_keys_cf(COLUMN_CELL)
            .map_err(|err| {
                error!("estimate_num_keys_cf error {}", err);
                RPCError::ckb_internal_error(err)
            })?;
```

**File:** resource/ckb.toml (L190-193)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
