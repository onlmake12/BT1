### Title
Unencrypted Peer Store Data Persisted to Browser IndexedDB in WASM Build — (`network/src/peer_store/browser.rs`, `network/src/peer_store/peer_store_db.rs`)

### Summary
The CKB WASM/browser node build persists peer store data (peer addresses, ban list, anchor peers) to browser IndexedDB without any encryption or access controls. Any JavaScript executing in the same browser origin — including via an XSS vulnerability in the hosting dApp — can read or overwrite this data, enabling network topology disclosure and peer connection manipulation (eclipse attack setup).

### Finding Description
When compiled for `target_family = "wasm"`, CKB stores its peer store state in browser IndexedDB via the `Storage` struct in `network/src/peer_store/browser.rs`. The `Storage::put` method writes raw (JSON-serialized) bytes directly into IndexedDB with no encryption layer. [1](#0-0) 

Three distinct data sets are persisted this way: `addr_manager.db` (known peer addresses and connection metadata), `ban_list.db` (banned peer addresses), and `anchors.db` (anchor peers used for eclipse-resistance): [2](#0-1) 

This is triggered periodically (every hour) and on node shutdown by `DumpPeerStoreService`: [3](#0-2) 

And loaded at startup via `NetworkState::from_config` (WASM path): [4](#0-3) 

IndexedDB, like `localStorage`, is accessible to any JavaScript running in the same browser origin. There are no access controls, no encryption, and no integrity checks on the stored data.

### Impact Explanation
An attacker who achieves JavaScript execution in the same origin as the CKB WASM node (e.g., via XSS in the hosting dApp) can:

1. **Read** `addr_manager.db` to enumerate all known peers, revealing the node's network topology and connection history.
2. **Overwrite** `ban_list.db` to clear all banned peers, allowing previously-banned malicious nodes to reconnect.
3. **Overwrite** `anchors.db` to replace anchor peers with attacker-controlled addresses, directly undermining the eclipse-resistance mechanism that anchors are designed to provide.

Manipulating anchor peers is the most severe consequence: anchors are specifically chosen to prevent eclipse attacks. Replacing them with attacker-controlled nodes biases future peer selection, enabling a targeted eclipse attack that can lead to transaction censorship or double-spend facilitation against the affected node.

### Likelihood Explanation
The CKB WASM node is explicitly designed to run inside browser-based dApps (introduced in v0.120.0). Browser dApps are a common XSS target. Any XSS vulnerability in the hosting web application — a realistic and common vulnerability class — provides the necessary JavaScript execution context. No privileged access, key material, or majority hashpower is required.

### Recommendation
- Encrypt peer store data before writing to IndexedDB using a key derived from a browser-side secret (e.g., via the Web Crypto API's `SubtleCrypto.encrypt`), so raw peer data is not readable by arbitrary JavaScript.
- At minimum, apply an HMAC integrity check over stored data so that tampered entries (e.g., a poisoned ban list or anchor list) are detected and rejected on load.
- Consider storing only non-sensitive peer metadata in IndexedDB and regenerating the peer list from bootnodes on each startup for the WASM build.

### Proof of Concept
1. Deploy a CKB WASM node inside a browser dApp.
2. Inject JavaScript into the dApp's origin (via XSS or a compromised script dependency).
3. From the injected script, open the IndexedDB database named after the peer store path and read the `main-store` object store:
   ```js
   const req = indexedDB.open("<peer_store_path>");
   req.onsuccess = e => {
     const db = e.target.result;
     const tx = db.transaction("main-store", "readwrite");
     const store = tx.objectStore("main-store");
     // Read addr_manager, ban_list, anchors keys
     store.getAll().onsuccess = ev => console.log(ev.target.result);
     // Overwrite anchors with attacker-controlled peer addresses
     store.put({ key: <anchors_key_bytes>, value: <malicious_anchors_json> });
   };
   ```
4. On next CKB node startup, `load_from_idb` loads the poisoned anchors, replacing the eclipse-resistance anchor set with attacker-controlled peers. [5](#0-4) [6](#0-5)

### Citations

**File:** network/src/peer_store/browser.rs (L69-87)
```rust
impl Storage {
    pub async fn new<P: AsRef<Path>>(path: P) -> Self {
        let factory = Factory::new().unwrap();
        let database_name = path.as_ref().to_str().unwrap().to_owned();
        let mut open_request = factory.open(&database_name, Some(1)).unwrap();
        open_request.on_upgrade_needed(move |event| {
            let database = event.database().unwrap();
            let store_params = ObjectStoreParams::new();

            let store = database
                .create_object_store(STORE_NAME, store_params)
                .unwrap();
            let mut index_params = IndexParams::new();
            index_params.unique(true);
            store
                .create_index("key", KeyPath::new_single("key"), Some(index_params))
                .unwrap();
        });
        let db = open_request.await.unwrap();
```

**File:** network/src/peer_store/browser.rs (L109-122)
```rust
                    CommandRequest::Put { kv } => {
                        let tran = db
                            .transaction(&[STORE_NAME], TransactionMode::ReadWrite)
                            .unwrap();
                        let store = tran.object_store(STORE_NAME).unwrap();

                        let key = serde_wasm_bindgen::to_value(&kv.key).unwrap();
                        let value = serde_wasm_bindgen::to_value(&kv).unwrap();
                        store.put(&value, Some(&key)).unwrap().await.unwrap();
                        assert_eq!(
                            TransactionResult::Committed,
                            tran.commit().unwrap().await.unwrap()
                        );
                        request.resp.send(CommandResponse::Put).unwrap();
```

**File:** network/src/peer_store/peer_store_db.rs (L171-230)
```rust
    #[cfg(target_family = "wasm")]
    pub async fn load_from_idb<P: AsRef<Path>>(path: P) -> Self {
        use crate::peer_store::browser::get_db;

        let addr_manager_path = path
            .as_ref()
            .join(DEFAULT_ADDR_MANAGER_DB)
            .to_str()
            .unwrap()
            .to_owned()
            .into_bytes();
        let ban_list_path = path
            .as_ref()
            .join(DEFAULT_BAN_LIST_DB)
            .to_str()
            .unwrap()
            .to_owned()
            .into_bytes();
        let anchors_path = path
            .as_ref()
            .join(DEFAULT_ANCHORS_DB)
            .to_str()
            .unwrap()
            .to_owned()
            .into_bytes();

        let db = get_db(path).await;

        let addr_manager = db
            .get(&addr_manager_path)
            .await
            .map_err(|err| debug!("Failed to get indexdb value, error: {:?}", err))
            .and_then(|data| {
                AddrManager::load(std::io::Cursor::new(data.unwrap_or_default()))
                    .map_err(|err| debug!("Failed to load peer store value, error: {:?}", err))
            })
            .unwrap_or_default();

        let ban_list = db
            .get(&ban_list_path)
            .await
            .map_err(|err| debug!("Failed to get indexdb value, error: {:?}", err))
            .and_then(|data| {
                BanList::load(std::io::Cursor::new(data.unwrap_or_default()))
                    .map_err(|err| debug!("Failed to load BanList value, error: {:?}", err))
            })
            .unwrap_or_default();

        let anchors = db
            .get(&anchors_path)
            .await
            .map_err(|err| debug!("Failed to get indexdb value, error: {:?}", err))
            .and_then(|data| {
                Anchors::load(std::io::Cursor::new(data.unwrap_or_default()))
                    .map_err(|err| debug!("Failed to load Anchors value, error: {:?}", err))
            })
            .unwrap_or_default();

        PeerStore::new(addr_manager, ban_list, anchors)
    }
```

**File:** network/src/peer_store/peer_store_db.rs (L282-289)
```rust
        async {
            let db = get_db(path).await;

            let _ignore = db.put(addr_manager_path.into_bytes(), addr_manager).await;
            let _ignore = db.put(ban_list_path.into_bytes(), ban_list).await;
            let _ignore = db.put(anchors_path.into_bytes(), anchors).await;
        }
    }
```

**File:** network/src/services/dump_peer_store.rs (L39-46)
```rust
    #[cfg(target_family = "wasm")]
    fn dump_peer_store(&self) {
        let path = self.network_state.config.peer_store_path();
        self.network_state.with_peer_store_mut(|peer_store| {
            let task = peer_store.dump_to_idb(path);
            p2p::runtime::spawn(task)
        });
    }
```

**File:** network/src/network.rs (L180-181)
```rust
        info!("Loading the peer store. This process may take a few seconds to complete.");
        let peer_store = Mutex::new(PeerStore::load_from_idb(config.peer_store_path()).await);
```
