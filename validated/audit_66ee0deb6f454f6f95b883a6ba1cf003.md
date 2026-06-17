### Title
`blockhash(req.blockNumber)` Always Returns Zero on Arbitrum/Optimism, Permanently Blocking `useBlockHash=true` Entropy Reveals — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The Entropy contract stores `block.number` at request time and later calls `blockhash(req.blockNumber)` during reveal. On L2s like Arbitrum and Optimism, `block.number` returns the **L1 block number**, which remains constant across many L2 transactions. Any user who calls `request()` with `useBlockHash = true` and then calls `reveal()` within the same L1 block will always receive a zero blockhash, causing an unconditional `BlockhashUnavailable` revert. The user has already paid the fee and cannot recover their random number until the L1 block advances.

---

### Finding Description

In `requestHelper`, the current `block.number` is stored into the request struct: [1](#0-0) 

During `revealHelper`, if `useBlockhash` is true, the contract fetches `blockhash(req.blockNumber)` and reverts if it is zero: [2](#0-1) 

The EVM `blockhash` opcode returns zero when the queried block number equals the **current** block number. On Arbitrum, `block.number` is the L1 block number (not the L2 block number), so it stays constant for the entire duration of an L1 block (~12 seconds, potentially dozens of L2 transactions). Any `reveal()` call submitted within that same L1 block will see `req.blockNumber == block.number`, get a zero blockhash, and revert unconditionally.

The legacy `request()` function still accepts `useBlockHash = true` from any caller: [3](#0-2) 

The `BlockhashUnavailable` error is defined in: [4](#0-3) 

The `blockNumber` field in the request struct is documented as storing `block.number`: [5](#0-4) 

---

### Impact Explanation

A user who calls `request(provider, userCommitment, true)` on Arbitrum (or Optimism) pays the required fee, receives a sequence number, and then cannot call `reveal()` until the L1 block advances. If the user or their contract attempts to reveal in the same L1 block (which is the normal, expected behavior on a fast L2), the call reverts with `BlockhashUnavailable`. The paid fee is not refunded. Additionally, if the user fails to reveal within 256 L1 blocks after the block advances, `blockhash` returns zero again (outside the 256-block window), making the request permanently unrevealable and the fee permanently lost.

---

### Likelihood Explanation

Entropy is deployed on multiple EVM chains. Arbitrum and Optimism are among the most widely used L2s. Any user or integrator who calls the legacy `request()` API with `useBlockHash = true` on these chains is affected. The condition (same L1 block) is the **default** scenario on a fast L2 — a user submitting a request and immediately trying to reveal in the next L2 transaction will almost certainly still be in the same L1 block. No special attacker action is required; normal usage triggers the revert.

---

### Recommendation

1. **Short-term**: Document that `useBlockHash = true` must not be used on L2s where `block.number` is the L1 block number (Arbitrum, Optimism, zkSync).
2. **Long-term**: Replace `block.number` with `block.timestamp` (or a combination of `block.timestamp` and `block.number`) when storing the block reference in `requestHelper`. `block.timestamp` advances with every L2 block on Arbitrum and Optimism, so `blockhash` would not be called with the current block's number. Alternatively, store the L2-native block number using a chain-specific opcode or precompile where available.

---

### Proof of Concept

1. Deploy or interact with Entropy on Arbitrum.
2. Call `request(provider, userCommitment, true)` — pay the required fee. The stored `req.blockNumber` is the current L1 block number, e.g., `N`.
3. In the next L2 transaction (still within L1 block `N`), call `reveal(provider, sequenceNumber, userContribution, providerContribution)`.
4. Inside `revealHelper`: `blockhash(N)` is called while `block.number == N`, returning `bytes32(0)`.
5. The check `if (_blockHash == bytes32(uint256(0))) revert EntropyErrors.BlockhashUnavailable()` fires.
6. The reveal reverts. The user's fee is consumed. The random number is inaccessible until L1 block `N+1` is mined.

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L262-263)
```text
        req.blockNumber = SafeCast.toUint64(block.number);
        req.useBlockhash = useBlockhash;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L322-336)
```text
    function request(
        address provider,
        bytes32 userCommitment,
        bool useBlockHash
    ) public payable override returns (uint64 assignedSequenceNumber) {
        EntropyStructsV2.Request storage req = requestHelper(
            provider,
            userCommitment,
            useBlockHash,
            false,
            0
        );
        assignedSequenceNumber = req.sequenceNumber;
        emit Requested(EntropyStructConverter.toV1Request(req));
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L411-421)
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
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyErrors.sol (L34-36)
```text
    // The blockhash is 0.
    // Signature: 0x92555c0e
    error BlockhashUnavailable();
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyStructsV2.sol (L55-62)
```text
        // The number of the block where this request was created.
        // Note that we're using a uint64 such that we have an additional space for an address and other fields in
        // this storage slot. Although block.number returns a uint256, 64 bits should be plenty to index all of the
        // blocks ever generated.
        uint64 blockNumber;
        // The address that requested this random number.
        address requester;
        // If true, incorporate the blockhash of blockNumber into the generated random value.
```
