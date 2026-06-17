### Title
Incomplete Balance Check in `updatePriceFeeds` Causes Keeper Transactions to Always Revert When Balance Is Between `pythFee` and `pythFee + keeperFee` - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`updatePriceFeeds` in `Scheduler.sol` performs an early balance check only against `pythFee`, but the function also deducts a separate keeper fee in `_processFeesAndPayKeeper`. When a subscription's balance satisfies `balanceInWei >= pythFee` but `balance