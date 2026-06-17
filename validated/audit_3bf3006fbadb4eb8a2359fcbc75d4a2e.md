### Title
`getMinimumBalance` Does Not Account for Gas Price Variability, Causing `updatePriceFeeds` to Fail During Gas Spikes — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.getMinimumBalance` returns a static value (`numPriceFeeds × minimumBalancePerFeed`) that does not incorporate `tx.gasprice`. However, `_processFeesAndPayKeeper` charges a keeper fee that scales directly with `tx.gasprice`. During gas price spikes, a subscription holding exactly the minimum balance will revert with `InsufficientBalance` inside `updatePriceFeeds`, making price updates impossible precisely when market volatility is highest.

---

### Finding Description

`getMinimumBalance` is the protocol's guarantee of how much ETH a subscription must hold to remain serviceable:

```solidity
// Scheduler.sol line 734-738
function getMinimumBalance(uint8 numPriceFeeds)
    external view override returns (uint256 minimumBalanceInWei)
{
    return uint256(numPriceFeeds) * this.getMinimumBalancePerFeed();
}
```

`minimumBalancePerFeed` is a fixed admin-set parameter with no awareness of current gas prices. [1](#0-0) 

The actual keeper fee charged inside `updatePriceFeeds` is computed in `_processFeesAndPayKeeper`:

```solidity
// Scheduler.sol lines 845-849
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
``` [2](#0-1) 

`gasCost` scales linearly with `tx.gasprice`. If gas prices spike (e.g., 10×), the `totalKeeperFee` for a single update can easily exceed `status.balanceInWei`, triggering:

```solidity
if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
``` [3](#0-2) 

The `GAS_OVERHEAD` constant is a rough static estimate of 30,000 gas units: [4](#0-3) 

This overhead is multiplied by `tx.gasprice` at execution time, making the actual cost unbounded relative to the static minimum balance. The `minimumBalancePerFeed` is set at initialization and can only be updated by the admin: [5](#0-4) 

---

### Impact Explanation

When gas prices spike, subscriptions holding exactly the minimum balance (the protocol-guaranteed serviceable amount) will fail to execute `updatePriceFeeds`. This breaks the core function of the Scheduler — automated, reliable price feed updates. The failure is worst during high market volatility, which is precisely when gas prices spike and when accurate, timely price updates are most critical for downstream consumers.

---

### Likelihood Explanation

Gas price spikes on Ethereum are common and well-documented (e.g., during NFT mints, token launches, or market crashes). The `minimumBalancePerFeed` is a static deployment-time parameter. Any subscription funded to exactly the minimum balance — which the protocol explicitly allows and enforces as sufficient — will fail to update during a gas spike. The admin can update `minimumBalancePerFeed` via `setMinimumBalancePerFeed`, but this is reactive and cannot prevent failures during sudden spikes. [6](#0-5) 

---

### Recommendation

`getMinimumBalance` should incorporate a gas-price-aware component. One approach: include a multiplier of `GAS_OVERHEAD × expectedGasPrice × numExpectedUpdates` in the minimum balance formula, where `expectedGasPrice` is either an admin-configurable parameter or derived from a gas price oracle. Alternatively, add a configurable gas price buffer multiplier (analogous to the slippage buffer in the referenced fix) so that the minimum balance always covers at least N updates at a pessimistic gas price.

---

### Proof of Concept

1. Admin deploys Scheduler with `minimumBalancePerFeed = X` (calibrated at, say, 10 gwei gas price).
2. User calls `createSubscription` with `msg.value = getMinimumBalance(2)` = `2 * X`. This succeeds.
3. Gas price spikes to 100 gwei (10× increase).
4. Keeper calls `updatePriceFeeds(subscriptionId, updateData)`.
5. Inside `_processFeesAndPayKeeper`: `gasCost = (actualGasUsed + 30000) * 100 gwei`. At 10× gas price, `totalKeeperFee` is ~10× the assumed cost.
6. `status.balanceInWei < totalKeeperFee` → revert `InsufficientBalance`.
7. Price feeds are not updated. The subscription is stuck until the user manually adds funds or gas prices drop. [7](#0-6) [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L16-30)
```text
    function _initialize(
        address admin,
        address pythAddress,
        uint128 minimumBalancePerFeed,
        uint128 singleUpdateKeeperFeeInWei
    ) internal {
        require(admin != address(0), "admin is zero address");
        require(pythAddress != address(0), "pyth is zero address");

        _state.pyth = pythAddress;
        _state.admin = admin;
        _state.subscriptionNumber = 1;
        _state.minimumBalancePerFeed = minimumBalancePerFeed;
        _state.singleUpdateKeeperFeeInWei = singleUpdateKeeperFeeInWei;
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L734-738)
```text
    function getMinimumBalance(
        uint8 numPriceFeeds
    ) external view override returns (uint256 minimumBalanceInWei) {
        // TODO: Consider adding a base minimum balance independent of feed count
        return uint256(numPriceFeeds) * this.getMinimumBalancePerFeed();
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L840-864)
```text
    function _processFeesAndPayKeeper(
        SchedulerStructs.SubscriptionStatus storage status,
        uint256 startGas,
        uint256 numPriceIds
    ) internal {
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

        // Pay keeper and update status
        (bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
        if (!sent) {
            revert SchedulerErrors.KeeperPaymentFailed();
        }
    }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L27-29)
```text
    /// Fixed gas overhead component used in keeper fee calculation.
    /// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
    uint256 public constant GAS_OVERHEAD = 30000;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerGovernance.sol (L82-89)
```text
    function setMinimumBalancePerFeed(uint128 newMinimumBalance) external {
        _authorizeAdminAction();

        uint oldBalance = _state.minimumBalancePerFeed;
        _state.minimumBalancePerFeed = newMinimumBalance;

        emit MinimumBalancePerFeedSet(oldBalance, newMinimumBalance);
    }
```
