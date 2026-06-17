### Title
`getMinimumBalance` Does Not Account for the Dynamic Pyth Protocol Fee, Allowing Over-Withdrawal That Renders Subscriptions Inoperable — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `getMinimumBalance` function in `Scheduler.sol` computes the withdrawal floor as `numPriceFeeds × minimumBalancePerFeed`, covering only the keeper-fee component. It omits the dynamic Pyth protocol fee (`pythFee`) that is also deducted on every `updatePriceFeeds` call. Because `withdrawFunds` uses this incomplete floor, a subscription manager can legally drain the balance to a level that is insufficient to execute even one more update, silently rendering the subscription inoperable.

---

### Finding Description

`updatePriceFeeds` deducts two distinct fee buckets in sequence:

**Step 1 — Pyth protocol fee** (dynamic, proportional to `updateData` size): [1](#0-0) 

**Step 2 — Keeper fee** (gas cost + per-feed flat fee): [2](#0-1) 

The total per-update cost is therefore `pythFee + gasCost + singleUpdateKeeperFeeInWei × numPriceIds`.

`getMinimumBalance`, however, only returns the keeper-fee component: [3](#0-2) 

`withdrawFunds` enforces this incomplete floor: [4](#0-3) 

After a withdrawal that leaves `balanceInWei = minimumBalance`, the next `updatePriceFeeds` call deducts `pythFee` first (line 305), reducing `balanceInWei` to `minimumBalance − pythFee`. The subsequent keeper-fee check at line 852 then compares this reduced balance against `totalKeeperFee ≈ minimumBalance`, and reverts with `InsufficientBalance`. The subscription is stuck: it holds the protocol-mandated minimum but cannot be updated.

---

### Impact Explanation

Any subscription manager (an ordinary transaction sender, no privilege required) can call `withdrawFunds` to drain the balance to exactly `getMinimumBalance(numFeeds)`. Because `pythFee` is not included in that floor, the subscription can no longer be updated by any keeper. Protocols that depend on this subscription for on-chain price data will silently consume stale prices, which can cause incorrect liquidations, mispriced trades, or other downstream financial harm in integrating DeFi applications. The subscription manager may trigger this inadvertently (believing the minimum balance is sufficient) or deliberately to grief a subscription they no longer control (e.g., after transferring management).

---

### Likelihood Explanation

The Pyth protocol fee is non-zero on all production deployments (`getUpdateFee` charges per update VAA). Any subscription manager who withdraws to the minimum balance — a natural action when reclaiming capital — will hit this condition. The `minimumBalancePerFeed` parameter is set by the admin and is documented only as covering keeper costs; there is no on-chain enforcement that it must also cover `pythFee`. The `TODO` comment at line 737 (`// TODO: Consider adding a base minimum balance independent of feed count`) further signals that the minimum balance calculation is known to be incomplete. [5](#0-4) 

---

### Recommendation

Include the Pyth protocol fee in the minimum balance floor. Since `pythFee` is dynamic (it depends on `updateData`), the safest approach is to add a configurable `minimumPythFeeBuffer` to `getMinimumBalance`:

```solidity
function getMinimumBalance(uint8 numPriceFeeds)
    external view override returns (uint256)
{
    return uint256(numPriceFeeds) * this.getMinimumBalancePerFeed()
         + _state.minimumPythFeeBuffer;   // covers at least one pythFee
}
```

Alternatively, expose `pyth.singleUpdateFeeInWei()` and multiply by `numPriceFeeds` to derive a dynamic Pyth-fee component, mirroring how `singleUpdateKeeperFeeInWei` is already handled.

---

### Proof of Concept

**Setup:**
- `minimumBalancePerFeed = 0.5 ETH` (admin-configured to cover keeper gas + flat fee)
- Subscription has 1 price feed → `minimumBalance = 0.5 ETH`
- `pythFee = 0.1 ETH` (from `pyth.getUpdateFee(updateData)`)
- `totalKeeperFee ≈ 0.5 ETH` (gas + `singleUpdateKeeperFeeInWei`)

**Attack steps:**

1. Manager calls `withdrawFunds(subscriptionId, balance − 0.5 ETH)`.
   - Check: `0.5 ETH − 0 ≥ minimumBalance (0.5 ETH)` → passes.
   - `balanceInWei = 0.5 ETH`.

2. Keeper calls `updatePriceFeeds(subscriptionId, updateData)`.
   - Line 295: `0.5 ETH ≥ pythFee (0.1 ETH)` → passes.
   - Line 305: `balanceInWei = 0.5 ETH − 0.1 ETH = 0.4 ETH`.
   - Line 852 (`_processFeesAndPayKeeper`): `0.4 ETH < totalKeeperFee (0.5 ETH)` → **REVERT `InsufficientBalance`**.

3. The subscription holds the protocol-mandated minimum balance yet cannot be updated. All consumers of this subscription now receive stale prices indefinitely.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L292-306)
```text
        uint256 pythFee = pyth.getUpdateFee(updateData);

        // If we don't have enough balance, revert
        if (status.balanceInWei < pythFee) {
            revert SchedulerErrors.InsufficientBalance();
        }

        // Parse the price feed updates with an acceptable timestamp range of [0, now+10s].
        // Note: We don't want to reject update data if it contains a price
        // from a market that closed a few days ago, since it will contain a timestamp
        // from the last trading period. Thus, we use a minimum timestamp of zero while parsing,
        // and we enforce the past max validity ourselves in _validateShouldUpdatePrices using
        // the highest timestamp in the update data.
        status.balanceInWei -= pythFee;
        status.totalSpent += pythFee;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L649-655)
```text
        if (params.isActive) {
            uint256 minimumBalance = this.getMinimumBalance(
                uint8(params.priceIds.length)
            );
            if (status.balanceInWei - amount < minimumBalance) {
                revert SchedulerErrors.InsufficientBalance();
            }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L734-739)
```text
    function getMinimumBalance(
        uint8 numPriceFeeds
    ) external view override returns (uint256 minimumBalanceInWei) {
        // TODO: Consider adding a base minimum balance independent of feed count
        return uint256(numPriceFeeds) * this.getMinimumBalancePerFeed();
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L845-857)
```text
        // Calculate fee components
        uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
        uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) *
            numPriceIds;
        uint256 totalKeeperFee = gasCost + keeperSpecificFee;

        // Check balance
        if (status.balanceInWei < totalKeeperFee) {
            revert SchedulerErrors.InsufficientBalance();
        }

        status.balanceInWei -= totalKeeperFee;
        status.totalSpent += totalKeeperFee;
```
