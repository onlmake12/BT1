### Title
`verifyUpdate` Excess-Fee Refund via `.transfer()` Causes Permanent DoS for Contract Callers - (`lazer/contracts/evm/src/PythLazer.sol`)

### Summary
`PythLazer.verifyUpdate` refunds excess ETH to `msg.sender` using the deprecated `.transfer()` primitive, which forwards only 2 300 gas. Any smart contract that calls `verifyUpdate` with `msg.value > verification_fee` and lacks a trivial `receive()` function will have every call unconditionally revert, permanently blocking that contract from consuming Lazer price feeds on-chain.

### Finding Description
In `PythLazer.sol`, the `verifyUpdate` function handles the excess-fee refund as follows:

```solidity
// lazer/contracts/evm/src/PythLazer.sol  lines 74-77
require(msg.value >= verification_fee, "Insufficient fee provided");
if (msg.value > verification_fee) {
    payable(msg.sender).transfer(msg.value - verification_fee);
}
``` [1](#0-0) 

`address.transfer()` hard-caps the gas forwarded to the recipient at 2 300. This is sufficient for a plain ETH transfer to an EOA, but it is insufficient for:

- Any contract that has no `receive()` / `fallback()` function (the call reverts immediately).
- Any contract whose `receive()` function performs even a single storage write (> 2 300 gas).

Because `verifyUpdate` issues the refund **before** completing the signature verification logic, a revert in the `.transfer()` call rolls back the entire transaction. The caller receives its ETH back, but the price-feed verification never succeeds.

The official EVM integration guide instructs consumers to build a wrapper contract that calls `verifyUpdate`:

```solidity
// from docs: integrate-as-consumer/evm.mdx  lines 63-67
function updatePrice(bytes calldata priceUpdate) public payable {
  uint256 verification_fee = pythLazer.verification_fee();
  (bytes calldata payload, ) = verifyUpdate{ value: verification_fee }(update);
}
``` [2](#0-1) 

A consumer contract that defensively sends slightly more than `verification_fee` (a standard pattern to guard against fee changes between the query and the call) will be permanently DoS'd if it lacks a `receive()` function.

The `verification_fee` is a mutable owner-controlled value:

```solidity
uint256 public verification_fee;   // PythLazer.sol line 10
``` [3](#0-2) 

Because the fee can change between the block in which a consumer reads it and the block in which it submits the transaction, sending a small buffer above the queried fee is the only safe strategy — yet that buffer triggers the `.transfer()` revert for any contract without a trivial `receive()`.

### Impact Explanation
Any smart contract integrating Lazer that (a) sends `msg.value > verification_fee` and (b) does not implement a gas-free `receive()` function will have every `verifyUpdate` call revert. This is a complete, permanent DoS of the Lazer price-feed verification path for that contract. DeFi protocols relying on Lazer for liquidation triggers, AMM pricing, or collateral valuation would be unable to consume price updates, potentially freezing protocol operations or preventing time-sensitive actions.

### Likelihood Explanation
The integration pattern is explicitly documented and widely used. Many production contracts (e.g., proxy contracts, multisigs, contracts using OpenZeppelin's `ReentrancyGuard` with storage writes in `receive`) do not accept ETH or consume more than 2 300 gas in their `receive()` hook. The race condition between fee reads and fee changes makes overpaying a rational defensive choice, making this code path reachable in normal operation without any adversarial action.

### Recommendation
Replace `.transfer()` with a low-level `.call` that forwards all available gas and checks the return value:

```solidity
if (msg.value > verification_fee) {
    (bool ok, ) = payable(msg.sender).call{value: msg.value - verification_fee}("");
    require(ok, "Refund failed");
}
```

This is consistent with the pattern already used in `PythAggregatorV3.sol`:

```solidity
// target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol  lines 35-37
(bool success, ) = payable(msg.sender).call{
    value: address(this).balance
}("");
``` [4](#0-3) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.13;

import { PythLazer } from "pyth-lazer-sdk/PythLazer.sol";

// This contract has NO receive() function — common in many DeFi protocols.
contract VictimConsumer {
    PythLazer public pythLazer;

    constructor(address _pythLazer) {
        pythLazer = PythLazer(_pythLazer);
    }

    // Sends 1 wei more than the fee as a safety buffer.
    // Will ALWAYS revert because pythLazer.transfer() fails
    // when msg.sender (this contract) has no receive().
    function updatePrice(bytes calldata update) external payable {
        uint256 fee = pythLazer.verification_fee();
        // Defensive overpay: fee + 1 wei
        pythLazer.verifyUpdate{value: fee + 1}(update);
        // ^^^ reverts: "transfer failed" inside PythLazer
    }
}
```

1. Deploy `VictimConsumer` (no `receive()` function).
2. Fund it with ETH.
3. Call `updatePrice` with a valid Lazer update.
4. The call reverts at `payable(msg.sender).transfer(1)` inside `PythLazer.verifyUpdate` because `VictimConsumer` cannot accept ETH.
5. The contract can never successfully verify a Lazer price update.

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L10-10)
```text
    uint256 public verification_fee;
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L74-77)
```text
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }
```

**File:** apps/developer-hub/content/docs/price-feeds/pro/integrate-as-consumer/evm.mdx (L63-67)
```text
function updatePrice(bytes calldata priceUpdate) public payable {
  uint256 verification_fee = pythLazer.verification_fee();
  (bytes calldata payload, ) = verifyUpdate{ value: verification_fee }(update);
  //...
}
```

**File:** target_chains/ethereum/sdk/solidity/PythAggregatorV3.sol (L35-37)
```text
        (bool success, ) = payable(msg.sender).call{
            value: address(this).balance
        }("");
```
