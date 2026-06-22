### Title
Unchecked `i64 as u64` Cast on SQL-Aggregated Capacity Produces Phantom Large Value — (`File: util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs`)

---

### Summary

`get_cells_capacity` fetches the SQL aggregate `CAST(SUM(output.capacity) AS BIGINT)` into a Rust `i64`, then unconditionally casts it to `u64`. If the signed aggregate is ever negative (SQL BIGINT overflow in SQLite, or a stored negative capacity value), the cast silently produces a phantom astronomically large capacity that is returned verbatim to any RPC caller.

---

### Finding Description

In `get_cells_capacity.rs`, the query selects:

```sql
CAST(SUM(output.capacity) AS BIGINT) AS total_capacity
``` [1](#0-0) 

The result is retrieved as `i64`:

```rust
.and_then(|row| row.try_get::<i64, _>("total_capacity").ok());
``` [2](#0-1) 

Then immediately cast to `u64` with no sign check:

```rust
let capacity = match capacity {
    Some(capacity) => capacity as u64,
    None => return Ok(None),
};
``` [3](#0-2) 

In Rust, `negative_i64 as u64` is a **bit-reinterpretation**, not a checked conversion. A value of `-1i64` becomes `u64::MAX` (18,446,744,073,709,551,615). There is no guard, no `try_from`, and no error path for a negative aggregate.

The capacity column is stored as `i64` in the database schema:

```rust
type OutputCellRow = (
    i32,
    i64,   // capacity stored as signed BIGINT
    ...
);
``` [4](#0-3) 

SQLite performs integer arithmetic in signed 64-bit space. If the running `SUM` wraps past `i64::MAX`, SQLite silently returns a negative BIGINT. PostgreSQL raises an overflow error instead, but the Rust code has no handling for either case — it simply casts whatever `i64` the DB returns.

---

### Impact Explanation

Any RPC caller invoking `get_cells_capacity` with a search key that matches a sufficiently large set of live cells would receive a fabricated, astronomically large capacity value (`u64::MAX` in the worst case). Downstream applications that use this value to:

- Gate whether a user "has enough capacity" to create new cells
- Display wallet balances
- Make automated on-chain decisions based on indexer data

…would operate on a completely wrong number, potentially authorizing operations that should be rejected or vice versa.

---

### Likelihood Explanation

The current total CKB issuance is approximately 3.36 × 10¹⁸ shannons, which is below `i64::MAX` (9.22 × 10¹⁸ shannons). Under normal conditions the aggregate will not overflow. However:

1. The code contains **no defensive check** — it is one supply-schedule change or one future epoch away from silent corruption.
2. SQLite's signed-integer SUM wraps silently; no error is surfaced to the caller.
3. An RPC caller controls the `search_key` filter, so they can craft queries targeting arbitrary subsets of cells. They cannot force the DB to overflow today, but the absence of a guard means the bug is latent and will activate without any code change if the aggregate ever goes negative.

---

### Recommendation

Replace the bare `as u64` cast with a checked conversion that returns an error on negative values:

```rust
let capacity = match capacity {
    Some(c) if c >= 0 => c as u64,
    Some(c) => return Err(Error::DB(format!(
        "total_capacity aggregate returned negative value: {}", c
    ))),
    None => return Ok(None),
};
```

Alternatively, use `u64::try_from(c).map_err(...)` to make the intent explicit and compiler-enforced.

---

### Proof of Concept

1. Deploy a CKB rich-indexer node backed by SQLite.
2. Populate the DB with cells whose `SUM(capacity)` exceeds `i64::MAX` (achievable in a test environment by directly inserting rows, or by waiting for future issuance epochs).
3. Call `get_cells_capacity` via RPC with a broad `search_key`.
4. Observe the returned `capacity` field equals `2^64 - (overflow_amount)` instead of the correct sum — a phantom value orders of magnitude larger than the real total. [5](#0-4)

### Citations

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

**File:** util/rich-indexer/src/indexer/insert.rs (L30-36)
```rust
type OutputCellRow = (
    i32,
    i64,
    (Vec<u8>, i16, Vec<u8>),
    Option<(Vec<u8>, i16, Vec<u8>)>,
    Vec<u8>,
);
```
