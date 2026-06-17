### Title
`blockhash(req.blockNumber)` Permanently Unavailable on Optimism Due to Rapid Block Production, Permanently Locking Entropy Requests — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy.requestHelper()` records `block.number` at request time. `Entropy.revealHelper()` later calls `blockhash(req.blockNumber)` when `req.useBlockhash == true`. On Optimism, every transaction occupies its own L2 block, so 256 blocks elapse in seconds. The `blockhash` opcode returns `bytes32(0)` for any block older than 256 blocks, causing `revealHelper` to unconditionally revert with `BlockhashUnavailable`. Because there is no cancellation or refund path, any request submitted with `useBlockhash = true` on Optimism is permanently irrecoverable.

---

### Finding Description

In `requestHelper()`, the current L2 block number is stored into the request struct:

```solidity
req.blockNumber = SafeCast.toUint64(block.number);
req.useBlockhash = useBlockhash;
``` [1](#0-0) 

During reveal, `revealHelper()` calls `blockhash` on that stored block number:

```solidity
if (req.useBlockhash) {
    bytes32 _blockHash = blockhash(req.blockNumber);
    if (_blockHash == bytes32(uint256(0)))
        revert EntropyErrors.BlockhashUnavailable();
    blockHash = _blockHash;
}
``` [2](#0-1) 

The inline comment documents the intended design:

> "This allows the user to select between two random numbers by executing the reveal function **in the same block** as the request, **or after 256 blocks**." [3](#0-2) 

Both windows are broken on Optimism:

- **Same-block reveal is impossible**: On Optimism each transaction is placed in its own L2 block, so `block.number` at reveal time is always strictly greater than `req.blockNumber`.
- **256-block window expires in seconds**: On Optimism, L2 blocks are produced at roughly one per transaction (sub-second cadence). 256 blocks pass in a matter of seconds, after which `blockhash(req.blockNumber)` returns `bytes32(0)` permanently.

The result is that `revealHelper` always reverts with `BlockhashUnavailable` for any request made with `useBlockhash = true` on Optimism. There is no `cancel`, `refund`, or timeout function anywhere in the contract. [4](#0-3) 

The `Request` struct in both `EntropyStructs` and `EntropyStructsV2` stores `blockNumber` as a `uint64`, confirming it is the raw `block.number` value with no chain-aware adjustment: [5](#0-4) 

---

### Impact Explanation

Any user who calls the public `request(provider, userCommitment, true)` function on an Optimism deployment of Entropy will:

1. Pay the full provider + Pyth fee (transferred to provider and protocol balances immediately at request time).
2. Receive a sequence number for a request that can never be revealed.
3. Have no mechanism to cancel the request or recover their fee.

The request remains in the active-request hash table indefinitely, consuming a storage slot and potentially displacing other requests into the overflow mapping. [6](#0-5) 

---

### Likelihood Explanation

- The `request()` function with `useBlockhash` is a public, permissionless entry point callable by any user.
- Pyth Entropy is already deployed on Optimism and other L2 chains with rapid block production.
- The newer `requestV2` / `requestWithCallback` explicitly hard-code `useBlockhash = false`, but the legacy `request()` function remains callable and exposes `useBlockhash` as a caller-controlled boolean.
- A user following older SDK documentation or integrating directly with the ABI can trivially trigger this path.

---

### Recommendation

For the `request()` function, either:

1. **Remove the `useBlockhash` parameter entirely** and always set `useBlockhash = false`, consistent with the approach taken in `requestV2`.
2. **Or**, if the blockhash feature must be preserved, replace `block.number` with `block.timestamp` for the availability check, and use a timestamp-based expiry window instead of a block-count window — making the behavior chain-agnostic.

Additionally, add a request cancellation / refund path so that users whose requests become permanently unrevealable can recover their fees.

---

### Proof of Concept

1. Deploy (or use the existing) Entropy contract on Optimism.
2. Call `request(provider, keccak256(abi.encodePacked(secret)), true)` with sufficient fee. The contract stores `req.blockNumber = block.number` (e.g., block 1,000,000) and `req.useBlockhash = true`.
3. Wait ~5 seconds (256+ Optimism L2 blocks elapse).
4. Call `reveal(provider, sequenceNumber, secret, providerContribution)`.
5. Inside `revealHelper`, `blockhash(1000000)` returns `bytes32(0)` because the block is more than 256 blocks old.
6. The transaction reverts with `BlockhashUnavailable`.
7. Repeat step 4 indefinitely — the result is always the same revert. The fee paid in step 2 is permanently lost.

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L233-239)
```text
        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L262-263)
```text
        req.blockNumber = SafeCast.toUint64(block.number);
        req.useBlockhash = useBlockhash;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L411-424)
```text
        if (req.useBlockhash) {
            bytes32 _blockHash = blockhash(req.blockNumber);

            // The `blockhash` function will return zero if the req.blockNumber is equal to the current
            // block number, or if it is not within the 256 most recent blocks. This allows the user to
            // select between two random numbers by executing the reveal function in the same block as the
            // request, or after 256 blocks. This gives each user two chances to get a favorable result on
            // each request.
            // Revert this transaction for when the blockHash is 0;
            if (_blockHash == bytes32(uint256(0)))
                revert EntropyErrors.BlockhashUnavailable();

            blockHash = _blockHash;
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L496-530)
```text
    function reveal(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override returns (bytes32 randomNumber) {
        EntropyStructsV2.Request storage req = findActiveRequest(
            provider,
            sequenceNumber
        );

        if (
            req.callbackStatus != EntropyStatusConstants.CALLBACK_NOT_NECESSARY
        ) {
            revert EntropyErrors.InvalidRevealCall();
        }

        if (req.requester != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }
        bytes32 blockHash;
        (randomNumber, blockHash) = revealHelper(
            req,
            userContribution,
            providerContribution
        );
        emit Revealed(
            EntropyStructConverter.toV1Request(req),
            userContribution,
            providerContribution,
            blockHash,
            randomNumber
        );
        clearRequest(provider, sequenceNumber);
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyStructsV2.sol (L55-63)
```text
        // The number of the block where this request was created.
        // Note that we're using a uint64 such that we have an additional space for an address and other fields in
        // this storage slot. Although block.number returns a uint256, 64 bits should be plenty to index all of the
        // blocks ever generated.
        uint64 blockNumber;
        // The address that requested this random number.
        address requester;
        // If true, incorporate the blockhash of blockNumber into the generated random value.
        bool useBlockhash;
```
