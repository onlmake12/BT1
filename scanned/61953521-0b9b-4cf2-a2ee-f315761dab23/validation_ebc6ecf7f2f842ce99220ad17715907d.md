Looking at the CKB codebase for an analog to the "incomplete state update before a global check" vulnerability class, I need to find a case where multiple items need state updates, only some get updated, and a check uses all items' states (including stale ones).

Let me examine the tx-pool's `remove_entry_and_descendants` and `remove_entry` interaction closely.