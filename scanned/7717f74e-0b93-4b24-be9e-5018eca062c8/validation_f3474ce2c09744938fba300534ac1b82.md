The code is fully readable. Let me trace the exact logic.

**The `update_block_template` closure (lines 295–301):**

```rust
let updated = |id| {
    if id != work_id || id == 0 {
        Some(work_id)
    } else {
        None
    }
};
```

`id` = current `current_work_id` (stored atomic). `work_id` = incoming template's work_id.

**Trace with repeated work_id=0 POSTs:**
- `current_work_id` initializes to `0` (line 163)
- Call 1: `id=0`, `work_id=0` → `id != work_id` = false, `id == 0` = **true** → returns `Some(0)` → `fetch_update` succeeds → `current_work_id` stays `0` → `Works::New` sent
- Call 2: same state, same result → `Works::New` sent again
- Call N: identical — the `id == 0` branch permanently bypasses deduplication as long as `current_work_id` remains 0, which it does when work_id=0 is always sent

**Does `notify_new_work` save it?** No. The `legacy_work` LRU cache (line 129) only contains parent hashes of *submitted* blocks. A fresh miner has an empty cache, so all flooded `Works::New` messages pass through to `notify_workers(WorkerMessage::NewWork {...})`, causing workers to restart their nonce search on every message.

**Authentication on the listen port?** None. The `handle` function (lines 358–369) performs zero authentication — it accepts any HTTP POST with parseable BlockTemplate JSON from any source.

---

### Title
Deduplication bypass in `Client::update_block_template` via `id == 0` guard allows unauthenticated work-flooding — (`miner/src/client.rs`)

### Summary
The `update_block_template` function uses an `AtomicU64::fetch_update` closure that unconditionally returns `Some(work_id)` whenever the stored `current_work_id` equals `0`. Because the atomic is initialized to `0` and a crafted template with `work_id=0` keeps it at `0`, every subsequent POST with `work_id=0` passes the guard and dispatches a `Works::New` to the worker channel. The miner's listen port has no authentication, so any network-reachable sender can trigger this indefinitely.

### Finding Description
`Client::current_work_id` is initialized to `0`. [1](#0-0) 

The deduplication closure in `update_block_template` is: [2](#0-1) 

The condition `id == 0` was intended to force dispatch of the very first template (since the node's first `work_id` is also `0`, making `id != work_id` false at startup). However, because setting `current_work_id` to `0` leaves it at `0`, the `id == 0` branch fires on every subsequent call with `work_id=0`, making deduplication permanently ineffective for that value.

The HTTP handler that feeds `update_block_template` has no authentication: [3](#0-2) 

Each `Works::New` that passes through is processed by `Miner::notify_new_work`, which only skips work whose parent hash is already in the `legacy_work` submission cache — a cache that is empty until a block is actually submitted: [4](#0-3) 

So every flooded `Works::New` reaches `notify_workers(WorkerMessage::NewWork {...})`, causing all worker threads to abandon their current nonce search and restart. [5](#0-4) 

### Impact Explanation
Workers are interrupted and restarted on every crafted POST. A high-rate flood of `work_id=0` templates causes workers to spend all their time restarting rather than searching nonces, effectively halting mining throughput. This is a targeted denial-of-service against the miner process with no block submission possible during the attack.

### Likelihood Explanation
The listen port is a plain TCP socket with no authentication. Any host that can reach the configured `listen` address (which may be `0.0.0.0` in public deployments) can exploit this with a simple HTTP client. No credentials, keys, or privileged access are required. The `work_id=0` value is the legitimate first value issued by the node's block assembler, so a crafted template is trivially constructed from any real template.

### Recommendation
Remove the `id == 0` special case from the closure. The correct deduplication logic is simply:

```rust
let updated = |id| {
    if id != work_id {
        Some(work_id)
    } else {
        None
    }
};
```

The initialization concern (first template also having `work_id=0`) is already handled correctly by this condition: when `current_work_id=0` and the first real template arrives with `work_id=0`, `0 != 0` is false and the update is skipped — but this is the correct behavior since the work hasn't changed. If the intent is to always dispatch the very first template, initialize `current_work_id` to a sentinel value like `u64::MAX` instead of `0`.

Additionally, the listen endpoint should require authentication (e.g., a shared secret token) to prevent unauthenticated parties from injecting templates.

### Proof of Concept
```rust
// Pseudocode: call update_block_template 1000 times with work_id=0
let client = Client::new(...); // current_work_id starts at 0
for _ in 0..1000 {
    let template = make_block_template(work_id = 0);
    client.update_block_template(template);
}
// Without the bug fix: new_work_tx receives 1000 Works::New messages
// With the bug fix:     new_work_tx receives 0 Works::New messages
//                       (first real work_id != 0 would receive exactly 1)
```

Or via HTTP:
```bash
# Craft minimal valid BlockTemplate JSON with work_id=0
TEMPLATE='{"work_id":"0x0", ...}'
for i in $(seq 1 1000); do
  curl -s -X POST http://<miner-listen-addr> \
    -H 'Content-Type: application/json' \
    -d "$TEMPLATE"
done
# Workers restart 1000 times; nonce submission rate drops to near zero
```

### Citations

**File:** miner/src/client.rs (L163-163)
```rust
            current_work_id: Arc::new(AtomicU64::new(0)),
```

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
