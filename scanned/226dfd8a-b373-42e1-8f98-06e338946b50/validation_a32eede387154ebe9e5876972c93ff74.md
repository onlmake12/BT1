Looking at the vulnerability class from the report — **a lookup function silently returns a default/wrong value when an item is not found, causing incorrect computation** — I need to find an analog in CKB where a lookup uses the wrong index or default, causing incorrect results in a security-critical path.

Let me examine the DAO calculator and its witness index parsing closely.