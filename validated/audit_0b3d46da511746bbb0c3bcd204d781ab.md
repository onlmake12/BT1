### Title
Tx-Pool State Inconsistency: Evicted Entries Permanently Lost When `record_entry_edges` Fails After `check_and_record_ancestors` Mutates Pool State — (File: tx-pool/src/component/pool_map.rs)

### Summary
In `add_entry`, the function `check_and_record_ancestors` can permanently evict existing pool entries **and** mutate the `links` data structure before `record_entry_edges` is called. If `record_entry_edges` then fails, there is no rollback. Evicted entries are permanently gone while `links` holds dangling references to the never-inserted new entry — a direct structural analog to the SortedTroves/TroveManager split: one sub-structure (`links`) believes the entry exists while the primary store (`entries`) does not. The developers explicitly acknowledge this with a `FIXME` comment.

### Finding Description

In `pool_map.rs`, `add_entry` executes three sequential, stateful operations with no rollback path:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;  // (1) evicts entries + mutates links
self.record_entry_edges(&entry)?;                        // (2) can fail → no rollback of (1)
self.insert_entry(&entry, status);                       // (3) never reached on (2) failure
``` [1](#0-0) 

**Step (1)** — `check_and_record_ancestors` — has two irreversible side effects when the ancestor count exceeds `max_ancestors_count` and cell-dep parents exist:

- It calls `remove_entry_and_descendants` on candidate entries

### Citations

**File:** tx-pool/src/component/pool_map.rs (L210-220)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
        trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
        evicts = self.check_and_record_ancestors(&mut entry)?;
        self.record_entry_edges(&entry)?;
        self.insert_entry(&entry, status);
        self.record_entry_descendants(&entry);
        self.track_entry_statics(None, Some(status));
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
        Ok((true, evicts))
```
