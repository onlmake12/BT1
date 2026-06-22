### Title
Hardcoded Trivially-Guessable Default PostgreSQL Password Exposes Rich Indexer to Unauthorized Access and Data Corruption - (File: `util/app-config/src/configs/rich_indexer.rs`)

### Summary
The CKB rich indexer's PostgreSQL backend is configured with a hardcoded default password of `"123456"`. This password is both the programmatic default in production code and the value shown verbatim in the official documentation. Any attacker who can reach the PostgreSQL port — including a network-adjacent attacker when operators follow the documented setup — can authenticate with this trivially-guessable credential, read all indexed chain data, and corrupt the indexer state to cause the CKB node's RPC to return false cell/transaction data to downstream callers.

### Finding Description
`RichIndexerConfig` in `util/app-config/src/configs/rich_indexer.rs` defines the default PostgreSQL password via `default_db_password()`:

```rust
fn default_db_password() -> String {
    "123456".to_string()
}
```

This default is applied to the `db_password` field through `#[serde(default = "default_db_password")]`, meaning any operator who omits `db_password` from their config — or who copies the example config verbatim — will use `"123456"`.

The same value is reproduced as the canonical example in both `resource/ckb.toml` (line 304) and `util/rich-indexer/README.md` (line 101):

```toml
db_password = "123456"
```

The password is interpolated directly into the PostgreSQL connection URL by `build_url_for_postgres` in `util/rich-indexer/src/store.rs`:

```rust
fn build_url_for_postgres(db_config: &RichIndexerConfig) -> String {
    db_config.db_type.to_string()
        + db_config.db_user.as_str()
        + ":"
        + db_config.db_password.as_str()
        + "@"
        + db_config.db_host.as_str()
        ...
}
```

No validation, warning, or entropy check is performed on `db_password` at startup or configuration load time.

### Impact Explanation
An attacker who authenticates to the PostgreSQL instance with the default credential gains full read/write access to the `ckb-rich-indexer` database. Write access allows the attacker to:

1. **Corrupt indexed cell/transaction/script data** — the rich indexer RPC methods (`get_cells`, `get_transactions`, `get_cells_capacity`) serve results directly from this database. Injected false rows cause the CKB node to return fabricated cell sets or balances to any RPC caller.
2. **Suppress or replay indexed entries** — deleting or duplicating rows causes wallets and applications that rely on the indexer to miss live UTXOs or double-count spent cells, enabling theft or denial-of-service at the application layer.
3. **Read all indexed private query patterns** — script args, lock hashes, and type hashes of all indexed addresses are exposed, breaking address-level privacy.

### Likelihood Explanation
The `util/rich-indexer/README.md` explicitly instructs operators to configure PostgreSQL with `db_password = "123456"` and to expose the database for "secondary development" via direct SQL access. Operators following this documentation will deploy a publicly-reachable PostgreSQL instance with the world's most common numeric password. The default `db_host = "127.0.0.1"` provides no protection once an operator changes it to a non-loopback address, which the documentation encourages. No runtime warning is emitted when the default password is in use.

### Recommendation
- **Short term**: Remove the hardcoded `"123456"` default. Either require `db_password` to be explicitly set (returning an error at startup if absent when `db_type = "postgres"`), or generate a random credential on first run and print it to the operator.
- **Long term**: Add a startup check that warns (or refuses to start) when `db_password` matches a known-weak value. Update the documentation to instruct operators to set a strong, unique password and to restrict PostgreSQL network exposure.

### Proof of Concept
1. Deploy a CKB node with `[indexer_v2.rich_indexer]` set to `db_type = "postgres"` and `db_host = "0.0.0.0"` (as suggested by the README for secondary development), omitting `db_password` or copying the example value.
2. From any network-reachable host, run:
   ```
   psql -h <node-ip> -U postgres -d ckb-rich-indexer
   Password: 123456
   ```
3. Authentication succeeds. Execute:
   ```sql
   UPDATE output SET capacity = 0 WHERE lock_code_hash = '<target_lock_hash>';
   ```
4. Any subsequent `get_cells_capacity` RPC call for the target address returns `0x0`, causing the wallet to display a zero balance despite the on-chain funds being intact.

---

**Root cause references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** util/app-config/src/configs/rich_indexer.rs (L49-51)
```rust
    /// The database password.
    #[serde(default = "default_db_password")]
    pub db_password: String,
```

**File:** util/app-config/src/configs/rich_indexer.rs (L84-86)
```rust
fn default_db_password() -> String {
    "123456".to_string()
}
```

**File:** util/rich-indexer/src/store.rs (L238-249)
```rust
fn build_url_for_postgres(db_config: &RichIndexerConfig) -> String {
    db_config.db_type.to_string()
        + db_config.db_user.as_str()
        + ":"
        + db_config.db_password.as_str()
        + "@"
        + db_config.db_host.as_str()
        + ":"
        + db_config.db_port.to_string().as_str()
        + "/"
        + db_config.db_name.as_str()
}
```

**File:** resource/ckb.toml (L299-304)
```text
# db_type = "postgres"
# db_name = "ckb-rich-indexer"
# db_host = "127.0.0.1"
# db_port = 5432
# db_user = "postgres"
# db_password = "123456"
```

**File:** util/rich-indexer/README.md (L96-102)
```markdown
db_type = "postgres"
db_name = "ckb-rich-indexer"
db_host = "127.0.0.1"
db_port = 5432
db_user = "postgres"
db_password = "123456"
```
```
