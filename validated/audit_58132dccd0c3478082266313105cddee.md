### Title
Unbounded Cache Bypass in `get_sys_info` Allows Repeated Full OS Process Enumeration via `get_overview(1)` — (`rpc/src/module/terminal.rs`)

---

### Summary

`get_sys_info` unconditionally skips its TTL cache whenever the `SYSTEM_INFO` bit flag (0x1) is set in the `refresh` parameter. Any caller who can reach the Terminal RPC endpoint can call `get_overview(1)` in a tight loop, triggering `System::new_all()` + `sys.refresh_all()` on every single request with no rate limiting or minimum re-enumeration interval.

---

### Finding Description

In `get_sys_info`, the cache guard is:

```rust
if !refresh.contains(RefreshKind::SYSTEM_INFO)
    && let Some(cached) = self.cache.get_sys_info()
{
    return Ok(cached);
}
``` [1](#0-0) 

When `refresh` has `SYSTEM_INFO` set, the entire `if` block is skipped — the cache is never consulted. Execution falls through unconditionally to:

```rust
let mut sys = System::new_all();
sys.refresh_all();
``` [2](#0-1) 

The cache is populated at the end of the function, but on the very next call with `refresh=1`, the cache check is again bypassed. The TTL of 5 seconds defined in `ttl::SYSTEM_INFO` is completely irrelevant when the flag is set. [3](#0-2) 

`System::new_all()` (from the `sysinfo` crate) allocates a new `System` struct and enumerates all OS processes. `sys.refresh_all()` then re-reads every process's memory, CPU, and I/O stats. Additionally, `SysDisks::new_with_refreshed_list()` and `SysNetworks::new_with_refreshed_list()` are called on every invocation. [4](#0-3) 

There is no per-method rate limiting in the Terminal RPC. The RPC config exposes only `max_request_body_size`, `threads`, and `rpc_batch_limit` — none of which throttle repeated calls to a single method. [5](#0-4) 

---

### Impact Explanation

Each `get_overview(1)` call forces a full OS-level process table scan. On a host with hundreds of processes (typical for a production server), this is a CPU- and memory-intensive syscall sequence. An attacker calling this in a tight loop can:

- Saturate one or more CPU cores with process enumeration work
- Cause repeated heap allocations proportional to the number of running processes and network interfaces
- Degrade CKB node responsiveness for legitimate operations

---

### Likelihood Explanation

The Terminal module is opt-in but has no authentication. Any client that can reach the RPC listen address can call `get_overview` with arbitrary `refresh` flags. The RPC is documented as accepting `0x1` as a valid `refresh` value with no documented frequency restriction. [6](#0-5) 

---

### Recommendation

Replace the unconditional cache bypass with a TTL-aware forced refresh: even when the `SYSTEM_INFO` flag is set, check whether the cached entry is younger than the TTL and return it if so. Alternatively, enforce a per-caller or global minimum interval (e.g., equal to `ttl::SYSTEM_INFO`) between forced refreshes of system info, rejecting or serving cached data for sub-TTL requests.

---

### Proof of Concept

```python
import requests, time

url = "http://127.0.0.1:8114"
payload = {"jsonrpc": "2.0", "method": "get_overview", "params": [1], "id": 1}

start = time.time()
for i in range(100):
    requests.post(url, json=payload)
elapsed = time.time() - start
print(f"100 forced-refresh calls in {elapsed:.2f}s")
# Compare against: params=[0] (cached path) — should be ~100x faster
```

Each iteration with `params=[1]` triggers `System::new_all()` + `sys.refresh_all()` with no cache protection. The cached path (`params=[0]` or `params=[null]`) returns in microseconds after the first call. [7](#0-6)

### Citations

**File:** rpc/src/module/terminal.rs (L27-27)
```rust
    pub const SYSTEM_INFO: Duration = Duration::from_secs(5);
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

**File:** rpc/src/module/terminal.rs (L500-570)
```rust
    fn get_sys_info(&self, refresh: RefreshKind) -> Result<SysInfo> {
        // Check cache first unless force refresh
        if !refresh.contains(RefreshKind::SYSTEM_INFO)
            && let Some(cached) = self.cache.get_sys_info()
        {
            return Ok(cached);
        }

        // Fetch fresh system data
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

        let global = Global {
            total_memory,
            used_memory,
            global_cpu_usage,
            disks,
            networks,
        };

        let process = sys
            .process(
                sysinfo::get_current_pid()
                    .map_err(|e| RPCError::custom(RPCError::CKBInternalError, e))?,
            )
            .ok_or_else(|| {
                RPCError::custom(RPCError::CKBInternalError, "failed to get current process")
            })?;

        let sys_disk_usage = process.disk_usage();
        let sys_info = SysInfo {
            global,
            cpu_usage: process.cpu_usage(),
            memory: process.memory(),
            disk_usage: DiskUsage {
                total_written_bytes: sys_disk_usage.total_written_bytes,
                written_bytes: sys_disk_usage.written_bytes,
                total_read_bytes: sys_disk_usage.total_read_bytes,
                read_bytes: sys_disk_usage.read_bytes,
            },
            virtual_memory: process.virtual_memory(),
        };

        // Cache the result
        self.cache.set_sys_info(sys_info.clone());
        Ok(sys_info)
    }
```

**File:** util/app-config/src/configs/rpc.rs (L40-44)
```rust
    pub max_request_body_size: usize,
    /// Number of RPC worker threads.
    pub threads: Option<usize>,
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
```
