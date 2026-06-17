### Title
Unchecked ETH Refund Return Value Silently Traps Excess Funds - (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

### Summary
`PythAggregatorV3.updateFeeds` performs a low-level `.call` to refund excess ETH to `msg.sender` after paying the Pyth update fee, but the `bool success` return value is explicitly captured and then never checked. If the refund call fails (e.g., the caller is a smart contract without a payable fallback), the function silently succeeds, permanently trapping the excess ETH inside the `PythAggregatorV3` contract with no recovery path.

### Finding Description
In `PythAggregatorV3.sol`, the `updateFeeds` function is a publicly callable, payable wrapper that:
1. Queries the required Pyth update fee via `pyth.getUpdateFee(priceUpdateData)`.
2. Calls `pyth.updatePriceFeeds{value: fee}(priceUpdateData)`.
3. Attempts to refund the remaining contract balance (`address(this).balance`) to `msg.sender`.

The refund at lines 35–37 is:

```solidity
// refund remaining eth
// solhint-disable-next-line no-unused-vars
(bool success, ) = payable(msg.sender).call{
    value: address(this).balance
}("");
```

The `// solhint-disable-next-line no-unused-vars` comment explicitly acknowledges that `success` is declared but intentionally left unchecked. If the low-level call fails for any reason — most commonly because `msg.sender` is a smart contract without a `receive()` or `payable fallback()` function — the failure is silently swallowed. The function returns normally, the price feeds are updated, and the excess ETH is permanently locked in the `PythAggregatorV3` instance.

There is no `withdraw` or sweep function on `PythAggregatorV3`, so the trapped ETH has no recovery path.

### Impact Explanation
Any ETH sent to `updateFeeds` beyond the exact Pyth fee is permanently lost if the caller is a contract that cannot receive ETH. Because `PythAggregatorV3` is published as the canonical Chainlink-compatible adapter in `@pythnetwork/pyth-sdk-solidity`, many integrators deploy it and call `updateFeeds` from their own smart contracts (e.g., keeper bots, DeFi vaults, automated rebalancers). These callers routinely overpay to avoid fee-estimation failures. If their contract lacks a payable fallback, every such call silently destroys the overpayment. The loss is proportional to the overpayment amount and accumulates across all calls.

### Likelihood Explanation
The likelihood is medium-high. `updateFeeds` is explicitly documented as a convenience wrapper. Integrators calling it from smart contracts (the dominant use case for a Chainlink-compatible adapter) will commonly overpay to guarantee the fee is covered. Smart contracts without a payable fallback are the norm in DeFi (e.g., Gnosis Safe multisigs, many proxy contracts). The `solhint-disable` comment confirms the unchecked state is a known code pattern, not an oversight that was later fixed.

### Recommendation
Check the return value and revert if the refund fails, or use a pull-payment pattern:

```solidity
// Option A: revert on failed refund
uint256 refund = address(this).balance;
if (refund > 0) {
    (bool success, ) = payable(msg.sender).call{value: refund}("");
    require(success, "ETH refund failed");
}

// Option B: pull-payment — track owed refunds and let callers withdraw
```

Remove the `// solhint-disable-next-line no-unused-vars` suppression comment once the return value is properly handled.

### Proof of Concept
1. Deploy `PythAggregatorV3` pointing at a live Pyth contract.
2. Deploy an attacker/integrator contract `Caller` that has **no** `receive()` or `payable fallback()`.
3. From `Caller`, call `PythAggregatorV3.updateFeeds{value: 1 ether}(priceUpdateData)` where the actual Pyth fee is, say, 1 wei.
4. Observe: `updateFeeds` returns without reverting. `Caller` receives 0 ETH back. `PythAggregatorV3.balance` is now `1 ether - 1 wei` with no way to recover it.

```solidity
contract Caller {
    // No receive() or fallback() — cannot accept ETH
    function trigger(address aggregator, bytes[] calldata data) external {
        PythAggregatorV3(aggregator).updateFeeds{value: 1 ether}(data);
        // Execution reaches here; 1 ether - fee is permanently stuck
    }
}
``` [1](#0-0)

### Citations

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L26-38)
```text
    function updateFeeds(bytes[] calldata priceUpdateData) public payable {
        // Update the prices to the latest available values and pay the required fee for it. The `priceUpdateData` data
        // should be retrieved from our off-chain Price Service API using the `hermes-client` package.
        // See section "How Pyth Works on EVM Chains" below for more information.
        uint fee = pyth.getUpdateFee(priceUpdateData);
        pyth.updatePriceFeeds{value: fee}(priceUpdateData);

        // refund remaining eth
        // solhint-disable-next-line no-unused-vars
        (bool success, ) = payable(msg.sender).call{
            value: address(this).balance
        }("");
    }
```
