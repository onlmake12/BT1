### Title
Signed/Unsigned Integer Type Confusion in `get_cells_capacity` RPC Capacity Aggregation — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

---

### Summary

The CKB rich-indexer stores cell capacity values (semantically `u64`) as signed `BIGINT`/`INTEGER` in the database, then aggregates them with `CAST(SUM(output.capacity) AS BIGINT)`, fetches the result as `i64`, and casts it to `u64` without any sign check. This is a direct analog of the reported pattern: using a signed type where an unsigned type is semantically required, with implicit casts masking the mismatch. If the signed sum overflows or a stored value is negative, the `i64 as u64` reinterpretation produces a wildly incorrect capacity value returned to any RPC caller of `get_cells_capacity`.

---

### Finding Description

**Step 1 — Storage: `u64` cast to `i64`**

In `build_output_cell_rows`, each cell's `u64` capacity is cast to `i64` before being inserted into the database:

```rust
// util/rich-indexer/src/indexer/insert.rs:543-546
let cell_capacity: u64 = cell.capacity().into();
let cell_row = (
    output_index as i32,
    cell_capacity as i64,   // ← u64 silently reinterpreted as i64
``` [1](#0-0) 

**Step 2 — Schema: signed column type**

Both database schemas declare `capacity` as a signed integer type:

- PostgreSQL: `capacity BIGINT NOT NULL` (signed 64-bit)
- SQLite: `capacity INTEGER NOT NULL` (signed 64-bit in SQLite's type system) [2](#0-1) [3](#0-2) 

**Step 3 — Aggregation: signed SUM cast to BIGINT**

The `get_cells_capacity` query explicitly casts the aggregate to a signed `BIGINT`:

```rust
// util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs:27
query_builder.field("CAST(SUM(output.capacity) AS BIGINT) AS total_capacity");
``` [4](#0-3) 

**Step 4 — Retrieval: `i64` fetched and cast to `u64` without sign check**

```rust
// util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs:191-193
.and_then(|row| row.try_get::<i64, _>("total_capacity").ok());
let capacity = match capacity {
    Some(capacity) => capacity as u64,   // ← negative i64 becomes huge u64
``` [5](#0-4) 

The same pattern appears when reading individual cell capacities back from the database:

```rust
// util/rich-indexer/src/indexer/insert.rs:600, 634
let capacity: i64 = row.get("capacity");
...
.capacity(capacity as u64)
``` [6](#0-5) [7](#0-6) 

---

### Impact Explanation

There are two failure modes:

1. **Single-cell overflow**: If any cell has `capacity > i64::MAX` (≈ 92.2 billion CKB), `cell_capacity as i64` produces a negative stored value. SQL `SUM` then subtracts rather than adds this cell's contribution, causing the reported total to be lower than the actual total.

2. **Aggregate overflow**: If the SQL `SUM` of signed `BIGINT` values exceeds `i64::MAX`, the behavior is database-dependent:
   - **PostgreSQL**: raises a runtime overflow error, causing the RPC to return an error instead of a result.
   - **SQLite**: wraps the sum to a negative value. The subsequent `capacity as u64` reinterpretation (Rust two's-complement cast) produces a value near `u64::MAX`, which is returned to the caller as the total capacity.

In both cases, the `get_cells_capacity` RPC returns an incorrect value to any caller — wallets, dApps, or tooling — that relies on it for balance display or capacity planning decisions.

---

### Likelihood Explanation

The total CKB genesis issuance is ~33.6 billion CKB (3.36 × 10¹⁸ shannons), and `i64::MAX` ≈ 9.22 × 10¹⁸ shannons (≈ 92.2 billion CKB). Under the current supply, no single cell or script-controlled aggregate can reach `i64::MAX`. This makes the overflow practically unreachable today.

However:
- The type confusion is a latent defect that grows more relevant as secondary issuance accumulates over decades.
- The `CAST(SUM(...) AS BIGINT)` is an unnecessary narrowing cast that introduces the overflow risk where none would exist with an unsigned or arbitrary-precision type.
- The code pattern is fragile: any future protocol change increasing supply or any misconfiguration could trigger the bug silently.
- The `convert_max_values_in_search_filter` function already acknowledges this signed/unsigned tension explicitly in a comment, confirming the design is aware of the mismatch but has not resolved it at the storage layer. [8](#0-7) 

---

### Recommendation

1. **Remove the `CAST(SUM(output.capacity) AS BIGINT)`** and instead use `SUM(output.capacity)` directly, fetching the result as a `NUMERIC`/`TEXT` type (PostgreSQL) or as a Rust `i128`/`u128` to avoid signed overflow.
2. **Store capacity as `TEXT` or `NUMERIC`** in the schema to faithfully represent the full `u64` range without sign-bit aliasing.
3. **Add a sign check** after fetching: if the retrieved `i64` is negative, treat it as an error rather than silently casting to `u64`.
4. **Separate the concerns**: the storage representation (`i64` for DB compatibility) and the semantic type (`u64` for CKB capacity) should be explicitly converted with checked arithmetic, not silent `as` casts.

---

### Proof of Concept

An RPC caller issues:
```json
{"method": "get_cells_capacity", "params": [{"script": {...}, "script_type": "lock"}]}
```

If the script controls cells whose `capacity` values sum to a value that, when stored as signed `BIGINT` and aggregated, overflows `i64::MAX` in SQLite, the SQL engine wraps the sum to a large negative `i64`. The Rust code at line 193 then executes `negative_i64 as u64`, producing a value near `u64::MAX` (e.g., `-1i64 as u64 == 18446744073709551615`). This incorrect value is returned in the `capacity` field of `IndexerCellsCapacity` to the caller with no error signal. [9](#0-8) [10](#0-9)

### Citations

**File:** util/rich-indexer/src/indexer/insert.rs (L543-546)
```rust
    let cell_capacity: u64 = cell.capacity().into();
    let cell_row = (
        output_index as i32,
        cell_capacity as i64,
```

**File:** util/rich-indexer/src/indexer/insert.rs (L600-600)
```rust
    let capacity: i64 = row.get("capacity");
```

**File:** util/rich-indexer/src/indexer/insert.rs (L634-634)
```rust
        .capacity(capacity as u64)
```

**File:** util/rich-indexer/resources/create_postgres_table.sql (L54-62)
```sql
CREATE TABLE IF NOT EXISTS output(
    id BIGSERIAL PRIMARY KEY,
    tx_id BIGINT NOT NULL,
    output_index INTEGER NOT NULL,
    capacity BIGINT NOT NULL,
    lock_script_id BIGINT,
    type_script_id BIGINT,
    data BYTEA
);
```

**File:** util/rich-indexer/resources/create_sqlite_table.sql (L54-62)
```sql
CREATE TABLE IF NOT EXISTS output(
    id INTEGER PRIMARY KEY,
    tx_id INTEGER NOT NULL,
    output_index INTEGER NOT NULL,
    capacity INTEGER NOT NULL,
    lock_script_id INTEGER,
    type_script_id INTEGER,
    data BLOB
);
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L27-27)
```rust
        query_builder.field("CAST(SUM(output.capacity) AS BIGINT) AS total_capacity");
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L187-195)
```rust
        let capacity = query
            .fetch_optional(&mut *tx)
            .await
            .map_err(|err| Error::DB(err.to_string()))?
            .and_then(|row| row.try_get::<i64, _>("total_capacity").ok());
        let capacity = match capacity {
            Some(capacity) => capacity as u64,
            None => return Ok(None),
        };
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L221-225)
```rust
        Ok(Some(IndexerCellsCapacity {
            capacity: capacity.into(),
            block_hash,
            block_number: block_number.into(),
        }))
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L296-300)
```rust
// This function is used to convert u64::max values to i64::max in an IndexerSearchKeyFilter instance.
// The primary reason for this conversion is the limitation of the relational database used by the rich-indexer.
// The database can only handle integers up to i64::max.
// Secondly, in our application, the range of i64 is sufficient for our needs, so converting u64::max to i64::max does not cause any loss of information.
// Therefore, before passing the filter to the rich-indexer, we need to convert u64::max values to i64::max.
```
