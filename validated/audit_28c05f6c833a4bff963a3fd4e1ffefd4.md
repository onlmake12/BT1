[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** tx-pool/src/component/orphan.rs (L41-45)
```rust
#[derive(Default, Debug, Clone)]
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
}
```

**File:** tx-pool/src/component/orphan.rs (L74-87)
```rust
    pub fn remove_orphan_tx(&mut self, id: &ProposalShortId) -> Option<Entry> {
        self.entries.remove(id).inspect(|entry| {
            debug!("remove orphan tx {}", entry.tx.hash());
            for out_point in entry.tx.input_pts_iter() {
                if let Some(ids_set) = self.by_out_point.get_mut(&out_point) {
                    ids_set.remove(id);

                    if ids_set.is_empty() {
                        self.by_out_point.remove(&out_point);
                    }
                }
            }
        })
    }
```

**File:** tx-pool/src/component/orphan.rs (L89-94)
```rust
    pub fn remove_orphan_txs(&mut self, ids: impl Iterator<Item = ProposalShortId>) {
        for id in ids {
            self.remove_orphan_tx(&id);
        }
        self.shrink_to_fit();
    }
```

**File:** util/src/linked_hash_set.rs (L122-188)
```rust
impl<T, S> LinkedHashSet<T, S>
where
    T: Eq + Hash,
    S: BuildHasher,
{
    /// Returns `true` if the set contains a value.
    ///
    /// ```
    /// use ckb_util::LinkedHashSet;
    ///
    /// let mut set: LinkedHashSet<_> = LinkedHashSet::new();
    /// set.insert(1);
    /// set.insert(2);
    /// set.insert(3);
    /// assert_eq!(set.contains(&1), true);
    /// assert_eq!(set.contains(&4), false);
    /// ```
    pub fn contains(&self, value: &T) -> bool {
        self.map.contains_key(value)
    }

    /// Returns the number of elements the set can hold without reallocating.
    pub fn capacity(&self) -> usize {
        self.map.capacity()
    }

    /// Returns the number of elements in the set.
    pub fn len(&self) -> usize {
        self.map.len()
    }

    /// Returns `true` if the set contains no elements.
    pub fn is_empty(&self) -> bool {
        self.map.is_empty()
    }

    /// Adds a value to the set.
    ///
    /// If the set did not have this value present, true is returned.
    ///
    /// If the set did have this value present, false is returned.
    pub fn insert(&mut self, value: T) -> bool {
        self.map.insert(value, ()).is_none()
    }

    /// Gets an iterator visiting all elements in insertion order.
    ///
    /// The iterator element type is `&'a T`.
    pub fn iter(&self) -> Iter<'_, T> {
        Iter {
            iter: self.map.keys(),
        }
    }

    /// Visits the values representing the difference, i.e., the values that are in `self` but not in `other`.
    pub fn difference<'a>(&'a self, other: &'a LinkedHashSet<T, S>) -> Difference<'a, T, S> {
        Difference {
            iter: self.iter(),
            other,
        }
    }

    /// Clears the set of all value.
    pub fn clear(&mut self) {
        self.map.clear();
    }
}
```
