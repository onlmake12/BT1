### Title
User-Controlled `gasLimit` Exceeding Block Gas Limit Permanently Locks Entropy Requests — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary

A user calling `requestV2` can supply a `gasLimit` up to `MAX_GAS_LIMIT = 655,350,000` (≈655M gas). On every deployed EVM chain (Ethereum ~30M, Polygon ~20M, Avalanche ~8M), this ceiling far exceeds the actual block gas limit. When the stored `gasLimit10k` exceeds what the Fortuna keeper can forward, `revealWithCallback` always reverts with `InsufficientGas`, leaving the request permanently stuck in `CALLBACK_NOT_STARTED` with no recovery path and the user's fee locked in the contract.

### Finding Description

`Entropy.sol` defines a hard ceiling: [1](#0-0) 

```solidity
uint32 public constant MAX_GAS_LIMIT =
    uint32(type(uint16).max) * TEN_THOUSAND;  // 65535 * 10000 = 655,350,000
```

Any `gasLimit` up to 655M passes `roundTo10kGas` without reverting: [2](#0-1) 

The stored value is used verbatim as the gas forwarded to the callback in `revealWithCallback`: [3](#0-2) 

After the `excessivelySafeCall`, the contract checks whether the calling context had enough gas to honour the limit. If not, it reverts the entire transaction: [4](#0-3) 

The revert condition is:

```
(startingGas * 31) / 32 ≤ req.gasLimit10k * 10_000
```

i.e., the keeper must supply `startingGas > (32/31) × gasLimit`. For a request with `gasLimit = 30,000,000` on Ethereum (block gas limit ≈ 30M), the keeper would need >30.97M gas — impossible within a single block. The transaction reverts, the `callbackStatus` write to `CALLBACK_IN_PROGRESS` is also reverted, and the request stays in `CALLBACK_NOT_STARTED` forever.

Because the only state transitions out of `CALLBACK_NOT_STARTED` require a successful `revealWithCallback` execution, and there is no cancel or refund function, the request and its fee are permanently frozen.

The fee paid by the attacker scales proportionally with the excess gas: [5](#0-4) 

For a provider with `defaultGasLimit = 100,000` and `feeInWei = 1 gwei`, a 30M-gas request costs only `(30M − 100k) / 100k × 1 gwei ≈ 299 gwei` extra — negligible on low-fee chains.

### Impact Explanation

- **Permanent DoS on individual requests**: Any request whose stored `gasLimit10k` exceeds the chain's block gas limit can never be fulfilled. The requester never receives their random number.
- **Fee loss**: The fee paid at request time is credited to the provider and Pyth treasury immediately; there is no refund path.
- **Keeper gas drain**: The Fortuna keeper will repeatedly attempt `revealWithCallback` for stuck requests, wasting gas on each failed attempt until it detects and blacklists the sequence number off-chain.
- **Storage slot exhaustion (amplified)**: Because stuck requests are never cleared, they permanently occupy slots in the fixed-size `_state.requests` array, potentially forcing legitimate requests into the overflow mapping. [6](#0-5) 

### Likelihood Explanation

The attack requires only a single `requestV2` call with a large `gasLimit` and the corresponding (small) fee. No privileged access is needed. The entry point is fully permissionless. On low-fee chains (Polygon, Avalanche, BNB Chain), the cost to create a permanently stuck request is a few cents. The `MAX_GAS_LIMIT` of 655M is documented publicly, making the attack surface discoverable. [7](#0-6) 

### Recommendation

1. **Cap `MAX_GAS_LIMIT` per deployment**: Set `MAX_GAS_LIMIT` to a value safely below the target chain's block gas limit (e.g., 80% of the block gas limit), either as an immutable set at construction time or as a governance-controlled parameter.
2. **Add a cancellation / refund path**: Allow the requester (or anyone after a timeout) to cancel a stuck request and reclaim the fee, analogous to the `CALLBACK_FAILED` recovery flow.
3. **Pre-flight gas check at request time**: In `requestHelper`, verify that `callbackGasLimit` does not exceed a chain-specific maximum before storing the request.

### Proof of Concept

1. Provider registers with `defaultGasLimit = 100_000` on Ethereum (block gas limit ≈ 30M).
2. Attacker calls:
   ```solidity
   uint32 maliciousGasLimit = 30_000_000; // just above Ethereum block gas limit
   uint256 fee = entropy.getFeeV2(provider, maliciousGasLimit);
   entropy.requestV2{value: fee}(provider, userRandom, maliciousGasLimit);
   ```
   This succeeds — `30_000_000 < MAX_GAS_LIMIT (655_350_000)`.
3. Fortuna keeper calls `revealWithCallback` for this sequence number. The check `(startingGas * 31) / 32 > 30_000_000` requires `startingGas > 30_967_741`, which exceeds the Ethereum block gas limit. The transaction reverts with `InsufficientGas`.
4. The request remains in `CALLBACK_NOT_STARTED` indefinitely. The requester's fee is locked. The keeper's retry loop wastes gas on every block. [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L73-74)
```text
    uint32 public constant MAX_GAS_LIMIT =
        uint32(type(uint16).max) * TEN_THOUSAND;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L358-372)
```text
    function requestV2(
        address provider,
        bytes32 userContribution,
        uint32 gasLimit
    ) public payable override returns (uint64) {
        EntropyStructsV2.Request storage req = requestHelper(
            provider,
            constructUserCommitment(userContribution),
            // If useBlockHash is set to true, it allows a scenario in which the provider and miner can collude.
            // If we remove the blockHash from this, the provider would have no choice but to provide its committed
            // random number. Hence, useBlockHash is set to false.
            false,
            true,
            gasLimit
        );
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L581-596)
```text
            uint256 startingGas = gasleft();
            (success, ret) = req.requester.excessivelySafeCall(
                // Warning: the provided gas limit below is only an *upper bound* on the gas provided to the call.
                // At most 63/64ths of the current context's gas will be provided to a call, which may be less
                // than the indicated gas limit. (See CALL opcode docs here https://www.evm.codes/?fork=cancun#f1)
                // Consequently, out-of-gas reverts need to be handled carefully to ensure that the callback
                // was truly provided with a sufficient amount of gas.
                uint256(req.gasLimit10k) * TEN_THOUSAND,
                256, // copy at most 256 bytes of the return value into ret.
                abi.encodeWithSelector(
                    IEntropyConsumer._entropyCallback.selector,
                    sequenceNumber,
                    provider,
                    randomNumber
                )
            );
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L621-659)
```text
            } else if (
                (startingGas * 31) / 32 >
                uint256(req.gasLimit10k) * TEN_THOUSAND
            ) {
                // The callback reverted for some reason.
                // We don't use ret to condition the behavior here (out-of-gas or other revert), as we have found that some user contracts
                // catch out-of-gas errors and revert with a different error.
                // In this case, ensure that the callback was provided with sufficient gas. Technically, 63/64ths of the startingGas is forwarded,
                // but we're using 31/32 to introduce a margin of safety.
                emit CallbackFailed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    userContribution,
                    providerContribution,
                    randomNumber,
                    ret
                );
                emit EntropyEventsV2.Revealed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    randomNumber,
                    userContribution,
                    providerContribution,
                    true,
                    ret,
                    SafeCast.toUint32(gasUsed),
                    bytes("")
                );
                req.callbackStatus = EntropyStatusConstants.CALLBACK_FAILED;
            } else {
                // Callback reverted by (potentially) running out of gas, but the calling context did not have enough gas
                // to run the callback. This is a corner case that can happen due to the nuances of gas passing
                // in calls (see the comment on the call above).
                //
                // (Note that reverting here plays nicely with the estimateGas RPC method, which binary searches for
                // the smallest gas value that causes the transaction to *succeed*. See https://github.com/ethereum/go-ethereum/pull/3587 )
                revert EntropyErrors.InsufficientGas();
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L780-793)
```text
        uint32 roundedGasLimit = uint32(roundTo10kGas(gasLimit)) * TEN_THOUSAND;
        if (
            provider.defaultGasLimit > 0 &&
            roundedGasLimit > provider.defaultGasLimit
        ) {
            // This calculation rounds down the fee, which means that users can get some gas in the callback for free.
            // However, the value of the free gas is < 1 wei, which is insignificant.
            uint128 additionalFee = ((roundedGasLimit -
                provider.defaultGasLimit) * provider.feeInWei) /
                provider.defaultGasLimit;
            return provider.feeInWei + additionalFee;
        } else {
            return provider.feeInWei;
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L962-973)
```text
    function roundTo10kGas(uint32 gas) internal pure returns (uint16) {
        if (gas > MAX_GAS_LIMIT) {
            revert EntropyErrors.MaxGasLimitExceeded();
        }

        uint32 gas10k = gas / TEN_THOUSAND;
        if (gas10k * TEN_THOUSAND < gas) {
            gas10k += 1;
        }
        // Note: safe cast here should never revert due to the if statement above.
        return SafeCast.toUint16(gas10k);
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyStructsV2.sol (L44-70)
```text
    struct Request {
        // Storage slot 1 //
        address provider;
        uint64 sequenceNumber;
        // The number of hashes required to verify the provider revelation.
        uint32 numHashes;
        // Storage slot 2 //
        // The commitment is keccak256(userCommitment, providerCommitment). Storing the hash instead of both saves 20k gas by
        // eliminating 1 store.
        bytes32 commitment;
        // Storage slot 3 //
        // The number of the block where this request was created.
        // Note that we're using a uint64 such that we have an additional space for an address and other fields in
        // this storage slot. Although block.number returns a uint256, 64 bits should be plenty to index all of the
        // blocks ever generated.
        uint64 blockNumber;
        // The address that requested this random number.
        address requester;
        // If true, incorporate the blockhash of blockNumber into the generated random value.
        bool useBlockhash;
        // Status flag for requests with callbacks. See EntropyConstants for the possible values of this flag.
        uint8 callbackStatus;
        // The gasLimit in units of 10k gas. (i.e., 2 = 20k gas). We're using units of 10k in order to fit this
        // field into the remaining 2 bytes of this storage slot. The dynamic range here is 10k - 655M, which should
        // cover all real-world use cases.
        uint16 gasLimit10k;
    }
```
