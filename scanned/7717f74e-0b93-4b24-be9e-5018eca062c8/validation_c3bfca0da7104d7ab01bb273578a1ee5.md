### Title
Silent Discard of Ban-List Persistence Errors in WASM Peer Store — (`network/src/peer_store/peer_store_db.rs`)

---

### Summary

The `dump_to_idb` function in the WASM peer-store backend explicitly discards the `Result` returned by every `db.put()` call using `let _ignore = …`. If any write to IndexedDB fails (quota exceeded, browser storage error, etc.), the ban list, address manager, and anchor peers are silently **not** persisted. On the next node start the ban list is empty, so previously-banned peers can immediately reconnect. This is a direct structural analog to the "unsafe ERC20 transfer" class: a critical operation's failure is swallowed and the caller proceeds as if it succeeded.

---

### Finding Description

`dump_to_idb` is the WASM-target counterpart of `dump_to_dir`. The non-WASM path returns `Result<(), Error>` and propagates every I/O failure to the caller:

```rust
// non-WASM — errors propagated
pub fn dump_to_dir<P: AsRef<Path>>(&self, path: P) -> Result<(), Error> { … }
```

The WASM path, however, returns `impl Future<Output = ()>` and discards all three write results:

```rust
// WASM — errors silently dropped
let _ignore = db.put(addr_manager_path.into_bytes(), addr_manager).await;
let _ignore = db.put(ban_list_path.into_bytes(), ban_list).await;
let _ignore = db.put(anchors_path.into_bytes(), anchors).await;
``` [1](#0-0) 

Because the return type is `()`, the caller in `dump_peer_store.rs` cannot observe a failure even if it wanted to. The root cause is that `db.put()` returns `Result<(), PeerStoreError>` (see `browser.rs` line 150) but the result is bound to `_ignore` and dropped. [2](#0-1) 

The non-WASM `dump_to_dir` correctly propagates every error: [3](#0-2) 

---

### Impact Explanation

The three silently-dropped writes cover:

| Write | Security consequence on failure |
|---|---|
| `ban_list` | Banned peers are forgotten; they reconnect freely after node restart |
| `addr_manager` | Peer-discovery state lost; minor liveness impact |
| `anchors` | Anchor peers lost; minor liveness impact |

The ban-list loss is the security-relevant consequence. A peer that was banned for misbehaviour (e.g., sending invalid blocks, flooding, eclipse attempts) can reconnect immediately after the WASM node restarts, bypassing the ban entirely.

---

### Likelihood Explanation

- The WASM (browser) CKB node is a supported deployment target.
- IndexedDB writes can fail due to browser storage-quota limits, private-browsing mode restrictions, or transient browser errors — all realistic conditions.
- An attacker who knows the target runs a browser node can deliberately trigger misbehaviour, wait for the node to restart (e.g., tab reload), and reconnect without penalty.
- No privileged access is required; any unprivileged peer that gets banned is a potential beneficiary.

---

### Recommendation

Change `dump_to_idb` to return `impl Future<Output = Result<(), PeerStoreError>>` and propagate errors from each `db.put()` call, mirroring the error-handling discipline of `dump_to_dir`:

```rust
// proposed fix
pub fn dump_to_idb<P: AsRef<Path>>(
    &self,
    path: P,
) -> impl std::future::Future<Output = Result<(), PeerStoreError>> + use<P> {
    async {
        let db = get_db(path).await;
        db.put(addr_manager_path.into_bytes(), addr_manager).await?;
        db.put(ban_list_path.into_bytes(), ban_list).await?;
        db.put(anchors_path.into_bytes(), anchors).await?;
        Ok(())
    }
}
```

The call site in `dump_peer_store.rs` should then log or handle the returned error.

---

### Proof of Concept

1. Run a CKB WASM node in a browser with IndexedDB storage near its quota limit.
2. Connect a peer that triggers a ban (e.g., sends a malformed message that causes `misbehave → Disconnect`).
3. The node calls `dump_to_idb`; the `db.put(ban_list_path…)` call fails due to quota; the error is silently dropped.
4. Reload the browser tab (node restart).
5. The previously-banned peer reconnects successfully — the ban list is empty. [4](#0-3) [2](#0-1)

### Citations

**File:** network/src/peer_store/peer_store_db.rs (L233-251)
```rust
    pub fn dump_to_dir<P: AsRef<Path>>(&self, path: P) -> Result<(), Error> {
        // create dir
        create_dir_all(&path)?;
        // dump file to a temporary sub-directory
        let tmp_dir = path.as_ref().join("tmp");
        create_dir_all(&tmp_dir)?;
        let tmp_addr_manager = tmp_dir.join(DEFAULT_ADDR_MANAGER_DB);
        let tmp_ban_list = tmp_dir.join(DEFAULT_BAN_LIST_DB);
        let tmp_anchors_list = tmp_dir.join(DEFAULT_ANCHORS_DB);
        self.addr_manager().dump(dump_open(&tmp_addr_manager)?)?;
        move_file(
            tmp_addr_manager,
            path.as_ref().join(DEFAULT_ADDR_MANAGER_DB),
        )?;
        self.ban_list().dump(dump_open(&tmp_ban_list)?)?;
        move_file(tmp_ban_list, path.as_ref().join(DEFAULT_BAN_LIST_DB))?;
        self.anchors().dump(dump_open(&tmp_anchors_list)?)?;
        move_file(tmp_anchors_list, path.as_ref().join(DEFAULT_ANCHORS_DB))?;
        Ok(())
```

**File:** network/src/peer_store/peer_store_db.rs (L254-289)
```rust
    #[cfg(target_family = "wasm")]
    pub fn dump_to_idb<P: AsRef<Path>>(
        &self,
        path: P,
    ) -> impl std::future::Future<Output = ()> + use<P> {
        use crate::peer_store::browser::get_db;
        let ban_list = self.ban_list().dump_data();
        let addr_manager = self.addr_manager().dump_data();
        let anchors = self.anchors().dump_data();

        let addr_manager_path = path
            .as_ref()
            .join(DEFAULT_ADDR_MANAGER_DB)
            .to_str()
            .unwrap()
            .to_owned();
        let ban_list_path = path
            .as_ref()
            .join(DEFAULT_BAN_LIST_DB)
            .to_str()
            .unwrap()
            .to_owned();
        let anchors_path = path
            .as_ref()
            .join(DEFAULT_ANCHORS_DB)
            .to_str()
            .unwrap()
            .to_owned();
        async {
            let db = get_db(path).await;

            let _ignore = db.put(addr_manager_path.into_bytes(), addr_manager).await;
            let _ignore = db.put(ban_list_path.into_bytes(), ban_list).await;
            let _ignore = db.put(anchors_path.into_bytes(), anchors).await;
        }
    }
```

**File:** network/src/peer_store/browser.rs (L150-155)
```rust
    pub async fn put(&self, key: Vec<u8>, value: Vec<u8>) -> Result<(), PeerStoreError> {
        let kv = KV { key, value };

        send_command(&self.chan, CommandRequest::Put { kv }).await;
        Ok(())
    }
```
