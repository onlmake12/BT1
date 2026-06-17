### Title
Scheduler Keeper Compensation Omits L1 Rollup Data Fee on OP Stack L2 Chains — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

The `_processFeesAndPayKeeper` function in `Scheduler.sol` compensates keepers solely based on L2 execution gas (`tx.gasprice * gasUsed + GAS_OVERHEAD * tx.gasprice`) plus a fixed admin-set per-feed fee (`singleUpdateKeeperFeeInWei`). On OP Stack L2 chains (Optimism, Base, Soneium, Unichain, etc.), transactions also incur a separate L1 data fee that is not reflected in `tx.gasprice`. Because `updatePriceFeeds` carries large calldata (Pyth VAA price update data), the uncompensated L1 data fee can far exceed the L2 execution fee, making keeper operation unprofitable and causing price feeds to go stale.

### Finding Description

`_processFeesAndPayKeeper` calculates the keeper payment as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
``` [1](#0-0) 

`GAS_OVERHEAD` is a fixed constant of `30000` gas units: [2](#0-1) 

On OP Stack chains, the total transaction cost is:

```
Total cost = L2 execution fee + L1 data fee
           = tx.gasprice * gasUsed + L1GasPrice * (calldata_bytes * 16 + overhead) * scalar
```

`tx.gasprice` only reflects the L2 execution fee. The L1 data fee is a separate charge deducted from the sender's balance by the `L1FeeVault` precompile and is invisible to the EVM's `tx.gasprice` opcode. The contract has no mechanism to query or compensate for this fee.

The `singleUpdateKeeperFeeInWei` is a fixed admin-set value: [3](#0-2) 

A fixed value cannot track the dynamic L1/L2 gas price ratio, which historically varies from ~10x to ~10⁹x on Optimism. When L1 gas prices spike (e.g., during Ethereum congestion), the fixed `singleUpdateKeeperFeeInWei` will be insufficient to cover the L1 data fee, making keeper operation unprofitable.

The Pyth Pulse Scheduler is explicitly intended for deployment on L2 chains including Optimism, Base, Soneium, Unichain, and others: [4](#0-3) 

The `updatePriceFeeds` function is called by keepers with large `updateData` calldata (Pyth VAA price update data, typically 1700+ bytes per feed), making the L1 data fee substantial. [5](#0-4) 

### Impact Explanation

When L1 gas prices are elevated relative to L2 gas prices (a frequent condition on OP Stack chains), keepers submitting `updatePriceFeeds` will pay more in L1 data fees than they receive in compensation. Rational keepers will stop submitting updates, causing subscriptions to go stale. Subscription managers' deposited funds are locked in the contract while their price feeds are not updated. This directly breaks the core functionality of Pyth Pulse: ensuring on-chain prices remain up-to-date.

### Likelihood Explanation

The Scheduler contract is deployed on OP Stack chains (Optimism, Base, Soneium, Unichain) where the L1 data fee is a separate, non-EVM-visible charge. The L1/L2 gas price ratio on these chains varies widely and frequently. Historical data from Optimism shows the ratio ranging from ~10x to ~10⁹x within short time windows. Any period of elevated Ethereum gas prices (which occur regularly during network congestion) will make keeper operation unprofitable. This is not a rare edge case — it is a structural property of OP Stack fee accounting.

### Recommendation

Query the OP Stack `GasPriceOracle` precompile (at `0x420000000000000000000000000000000000000F`) to obtain the L1 data fee for the current transaction's calldata, and include it in the keeper compensation. Alternatively, use a chain-specific L1 fee estimation approach similar to what Perennial v2 implemented after this class of bug was reported (see https://github.com/equilibria-xyz/root/pull/74 and https://github.com/equilibria-xyz/root/pull/76).

For Arbitrum, the L1 fee is embedded in the gas accounting and partially captured by `tx.gasprice * gasUsed`, but the `singleUpdateKeeperFeeInWei` should still be set appropriately per chain.

### Proof of Concept

1. Deploy the Scheduler on Optimism mainnet with a `singleUpdateKeeperFeeInWei` calibrated at current gas prices.
2. Wait for an Ethereum gas price spike (e.g., L1 base fee rises from 10 Gwei to 100 Gwei while L2 base fee remains at 0.001 Gwei).
3. A keeper calls `updatePriceFeeds` with 2 price feeds (calldata ~3400 bytes of VAA data).
4. The keeper receives: `(gasUsed + 30000) * tx.gasprice + singleUpdateKeeperFeeInWei * 2`
5. The keeper actually paid: L2 execution fee + L1 data fee ≈ L2 execution fee + `100 Gwei * (3400 * 16 + 3100) * 0.684` ≈ L2 execution fee + ~3.9M Gwei ≈ 0.0039 ETH in L1 fees alone.
6. The `singleUpdateKeeperFeeInWei` (a fixed value set at deployment time) does not cover this dynamic L1 cost.
7. Keepers stop submitting updates; subscriptions go stale. [6](#0-5)

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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L27-29)
```text
    /// Fixed gas overhead component used in keeper fee calculation.
    /// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
    uint256 public constant GAS_OVERHEAD = 30000;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerGovernance.sol (L68-75)
```text
    function setSingleUpdateKeeperFeeInWei(uint128 newFee) external {
        _authorizeAdminAction();

        uint oldFee = _state.singleUpdateKeeperFeeInWei;
        _state.singleUpdateKeeperFeeInWei = newFee;

        emit SingleUpdateKeeperFeeSet(oldFee, newFee);
    }
```

**File:** contract_manager/src/store/contracts/EvmExecutorContracts.json (L99-121)
```json
    "chain": "arbitrum",
    "type": "EvmExecutorContract"
  },
  {
    "address": "0x6E7D74FA7d5c90FEF9F0512987605a6d546181Bb",
    "chain": "optimism",
    "type": "EvmExecutorContract"
  },
  {
    "address": "0x87047526937246727E4869C5f76A347160e08672",
    "chain": "blast",
    "type": "EvmExecutorContract"
  },
  {
    "address": "0x5744Cbf430D99456a0A8771208b674F27f8EF0Fb",
    "chain": "zetachain",
    "type": "EvmExecutorContract"
  },
  {
    "address": "0xf0a1b566B55e0A0CB5BeF52Eb2a57142617Bee67",
    "chain": "base",
    "type": "EvmExecutorContract"
  },
```
