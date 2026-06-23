The code confirms this is a real vulnerability. Here is the full analysis:

---

### Title
Tautological `work_id == 0` Condition Floods Unbounded `new_work_tx` Channel, Causing Miner OOM — (`miner/src/client.rs`)

### Summary
When the miner runs in notify mode, any caller with TCP access to the listen address can POST `BlockTemplate` JSON with `work_id = 0x0` in a tight loop. The deduplication guard in `update_block_template` becomes a tautology for `work_id = 0`, so every request unconditionally enqueues a `Works::New` entry into the **unbounded** crossbeam channel, growing it without limit until the process is OOM-killed.

### Finding Description

**1. The channel is unbounded.**

In `ckb-bin/src/subcommand/miner.rs` line 12, the channel is created with `crossbeam_channel::unbounded()`: [1](#0-0) 

`ckb_channel` is a direct re-export of `crossbeam_channel`, confirming no capacity limit exists: [2](#0-1) 

**2. The deduplication condition is a tautology when `work_id = 0`.**

`update_block_template` uses `fetch_update` with this closure: [3](#0-2) 

When the attacker supplies `work_id = 0`, the closure evaluates `(id != 0) || (id == 0)`, which is `true` for every possible value of `id`. `fetch_update` therefore always returns `Ok`, and the send always executes: [4](#0-3) 

**3. The notify endpoint has no authentication, rate limiting, or connection cap.**

`handle` reads the body and calls `update_block_template` with zero validation: [5](#0-4) 

`listen_block_template_notify` accepts connections in an unbounded loop with no per-IP or per-second limit: [6](#0-5) 

### Impact Explanation
An attacker who can reach the miner's notify TCP port sends HTTP POST requests with a valid `BlockTemplate` JSON body where `work_id = "0x0"` as fast as the network allows. Each request enqueues one `Works::New(Work)` item (a full block template struct) into the unbounded channel. The consumer (`Miner::run`) processes one item per mining iteration; it cannot drain the channel at the rate of a tight HTTP loop. Heap usage grows monotonically until the OS OOM-killer terminates the miner process. This is a **local RPC API crash** (miner process crash). 

### Likelihood Explanation
The notify listen address is operator-configured. If bound to `0.0.0.0` or any non-loopback interface (the documented example in the log message at line 216 shows `http://{addr}`), any network-adjacent host can trigger this. Even on loopback, any local unprivileged process can trigger it. No credentials, PoW, or special privileges are required — only the ability to open a TCP connection and send HTTP POST. [7](#0-6) 

### Recommendation
Two independent fixes are needed:

1. **Fix the tautology**: The intent of `id == 0` is to force an update when the stored ID is zero (initial state), not when the incoming `work_id` is zero. The condition should be `id != work_id || id == 0` where the `id == 0` branch guards the *stored* value, which is already what `id` represents. The bug is that when `work_id` is also `0`, the second clause makes the whole expression unconditionally true. A correct guard would be: `if id != work_id { Some(work_id) } else { None }` (removing the special-case entirely, or changing it to `if id == 0 && work_id == 0 { None }`).

2. **Bound the channel**: Replace `unbounded()` with `bounded(N)` (e.g., `bounded(4)`) so that a slow consumer naturally applies back-pressure and excess sends are dropped rather than queued forever. [8](#0-7) 

### Proof of Concept
```bash
# Craft a minimal valid BlockTemplate JSON with work_id = 0x0
TEMPLATE='{"version":"0x0","compact_target":"0x1a08a97e","current_time":"0x...","number":"0x1","epoch":"0x...","parent_hash":"0x...","cycles_limit":"0x...","bytes_limit":"0x...","uncles_count_limit":"0x2","uncles":[],"transactions":[],"proposals":[],"cellbase":{"hash":"0x...","cycles":null,"min_fee_rate":"0x0","time_added_to_pool":null,"transaction":{"cell_deps":[],"header_deps":[],"inputs":[{"previous_output":{"index":"0xffffffff","tx_hash":"0x..."},"since":"0x0"}],"outputs":[],"outputs_data":[],"version":"0x0","witnesses":["0x..."]}},"work_id":"0x0","dao":"0x..."}'

# Flood the notify endpoint in a tight loop
while true; do
  curl -s -X POST http://127.0.0.1:<notify_port> \
    -H 'Content-Type: application/json' \
    -d "$TEMPLATE" &
done

# Monitor miner RSS growth
watch -n1 'ps -o pid,rss,comm -p $(pgrep ckb-miner)'
```

Expected: `new_work_tx` channel depth grows unboundedly, miner RSS climbs until OOM kill. [8](#0-7)

### Citations

**File:** ckb-bin/src/subcommand/miner.rs (L12-12)
```rust
    let (new_work_tx, new_work_rx) = unbounded();
```

**File:** util/channel/src/lib.rs (L1-5)
```rust
//! Reexports `crossbeam_channel` to uniform the dependency version.
pub use crossbeam_channel::{
    Receiver, RecvError, RecvTimeoutError, Select, SendError, Sender, TrySendError, after, bounded,
    select, tick, unbounded,
};
```

**File:** miner/src/client.rs (L206-225)
```rust
        if let Some(addr) = self.config.listen {
            ckb_logger::info!("listen notify mode : {}", addr);
            ckb_logger::info!(
                r#"
Please note that ckb-miner runs in notify mode. \
You should configure the corresponding information in CKB block assembler, \
for example:

[block_assembler]
...
notify = ["http://{}"]

Otherwise ckb-miner will malfunction and stop submitting valid blocks after a certain period.
"#,
                addr
            );
            self.handle.spawn(async move {
                client.listen_block_template_notify(addr).await;
            });
            self.blocking_fetch_block_template();
```

**File:** miner/src/client.rs (L240-270)
```rust
        loop {
            let client = self.clone();
            let handle = service_fn(move |req| handle(client.clone(), req));
            tokio::select! {
                conn = listener.accept() => {
                    let (stream, _) = match conn {
                        Ok(conn) => conn,
                        Err(e) => {
                            info!("accept error: {}", e);
                            tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                            continue;
                        }
                    };
                    let stream = hyper_util::rt::TokioIo::new(Box::pin(stream));
                    let conn = server.serve_connection_with_upgrades(stream, handle);

                    let conn = graceful.watch(conn.into_owned());
                    tokio::spawn(async move {
                        if let Err(err) = conn.await {
                            info!("connection error: {}", err);
                        }
                    });
                },
                _ = stop_rx.cancelled() => {
                    info!("Miner client received exit signal. Exit now");
                    break;
                }
            }
        }
        drop(listener);
        graceful.shutdown().await;
```

**File:** miner/src/client.rs (L293-312)
```rust
    fn update_block_template(&self, block_template: BlockTemplate) {
        let work_id = block_template.work_id.into();
        let updated = |id| {
            if id != work_id || id == 0 {
                Some(work_id)
            } else {
                None
            }
        };
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
