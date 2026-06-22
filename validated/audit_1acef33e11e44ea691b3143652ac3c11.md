Looking at the call chain for `get_block_template`, I found a clear analog: caller-specified parameters are accepted at the RPC layer but silently dropped before reaching the template generation logic — the system always returns a cached constant value regardless of what the caller requests.

Let me verify the service layer to confirm the parameter drop.