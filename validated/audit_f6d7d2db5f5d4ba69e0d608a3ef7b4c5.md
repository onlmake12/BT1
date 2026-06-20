Looking at the vulnerability class — **accounting mismatch where a secondary path modifies a resource but a pre-computed counter snapshot is written back, ignoring intermediate decrements** — I need to find an analog in CKB's tx-pool where a size/cycles accumulator is updated asymmetrically.

Let me trace the exact code path in `PoolMap::add_entry`.