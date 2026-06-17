### Title
Unchecked Return Value of ETH Refund in `updateFeeds()` Silently Traps Excess User Funds - (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

---

### Summary

`PythAggregatorV3.updateFeeds()` attempts to refund excess ETH to `msg.sender` after paying the Pyth update fee, but the return value of the low-level `.call{}("")` is captured and never checked. If the refund fails (e.g., `msg.sender` is a contract without a `receive()` fallback, or one that reverts on ETH receipt), the function silently succeeds and the excess ETH is permanently locked in the `PythAggregatorV3` contract.

---

### Finding Description

In `PythAggregatorV3.updateFeeds()`:

```solidity
function updateFeeds(bytes[] calldata priceUpdateData) public payable {
    uint fee = pyth.getUpdateFee(priceUpdateData);
    pyth.updatePriceFeeds{value: fee}(priceUpdateData);

    // refund remaining eth
    // solhint-disable-next-line no-unused-vars
    (bool success, ) = payable(msg.sender).call{
        value: address(this).balance
    }("");
}
```

The `success` boolean returned by the low-level call is explicitly captured but never acted upon — there is no `require(success, ...)` guard. If the ETH transfer to `msg.sender` fails for any reason, the function returns normally, the excess ETH stays in the contract, and the caller has no recourse. [1](#0-0) 

---

### Impact Explanation

Any caller who sends `msg.value > getUpdateFee(priceUpdateData)` and whose address cannot receive ETH (e.g., a smart contract without a `receive()` or `fallback()` function, or one that reverts on ETH receipt) will permanently lose the excess ETH. The ETH accumulates in the `PythAggregatorV3` contract with no withdrawal mechanism. This constitutes a direct, irreversible loss of user funds.

---

### Likelihood Explanation

`PythAggregatorV3` is a public SDK contract intended to be integrated by third-party protocols. Integrators commonly call `updateFeeds()` from their own smart contracts (e.g., keepers, routers, aggregators) that may not implement ETH receipt. The `updateFeeds()` function is `public payable` with no restriction on callers. Any overpayment — whether accidental (gas estimation rounding) or by design — triggers the silent failure path. The `// solhint-disable-next-line no-unused-vars` comment confirms the developer was aware of the unused `success` variable but did not add a revert guard.

---

### Recommendation

Replace the unchecked low-level call with a checked version:

```solidity
(bool success, ) = payable(msg.sender).call{value: address(this).balance}("");
require(success, "ETH refund failed");
```

Alternatively, only attempt the refund when there is actually excess ETH to return:

```solidity
uint256 excess = address(this).balance;
if (excess > 0) {
    (bool success, ) = payable(msg.sender).call{value: excess}("");
    require(success, "ETH refund failed");
}
```

---

### Proof of Concept

1. Deploy a contract `Caller` that has no `receive()` function and calls `PythAggregatorV3.updateFeeds{value: 1 ether}(data)`.
2. `getUpdateFee(data)` returns, say, `1 wei`.
3. `pyth.updatePriceFeeds{value: 1 wei}(data)` succeeds.
4. The refund call `payable(Caller).call{value: 1 ether - 1 wei}("")` fails because `Caller` has no `receive()`.
5. `success == false` is silently ignored; `updateFeeds()` returns without reverting.
6. `~1 ether` is permanently locked in `PythAggregatorV3`. [2](#0-1)

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
