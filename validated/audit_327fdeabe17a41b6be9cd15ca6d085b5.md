Looking at the vulnerability class — **list/collection initialization without duplicate or sentinel validation** — I need to find Pyth production code where a collection is populated from a caller-supplied array without per-element uniqueness or zero-value checks.

Let me check the Stylus wormhole `store_gs` and `submit_new_guardian_set` implementations more carefully.