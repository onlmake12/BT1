### Title
Echo `executeCallback` Delivers Callback to `address(0)` for Overflow-Mapped Requests, Permanently Losing User Funds — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

In `Echo.sol`, `executeCallback` calls `clearRequest` before invoking the user's callback. For requests that have been evicted to the overflow mapping (`_state.requestsOverflow`), `clearRequest` issues a `delete` on the mapping entry, zeroing all struct fields — including `req.requester` and `req.callbackGasLimit`. The subsequent callback is then dispatched to `address(0)` with 0 gas. Because `address(0)` has no code, the low-level call succeeds silently, the `PriceUpdate