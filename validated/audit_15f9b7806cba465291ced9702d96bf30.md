### Title
Missing Bounds Checks in Assembly-Based Byte Readers Allow Silent Out-of-Bounds Memory Reads - (`File: lazer/contracts/evm/src/PythLazerLib.sol`)

### Summary

The `PythLazerLib` library exposes `public` parsing functions (`parsePayloadHeader`, `parseFeedHeader`, `parseFeedProperty`, `parseFeedValueUint64`, `parseUpdateFromPayload`, etc.) that internally call four assembly-based byte-reader helpers (`_readBytes1`, `_readBytes2`, `_readBytes4`, `_readBytes8`). None of these helpers perform any bounds check before executing `mload`. If `pos` is at or beyond `data.length`, the `mload` silently reads from memory beyond the array boundary, returning whatever resides there (typically zero-padded EVM free memory, but potentially adjacent in-memory data). This is the direct analog of the Basin `getBytes32FromBytes()` off-by-one: both perform unchecked assembly reads that silently return incorrect data instead of reverting.

---

### Finding Description

All four low-level readers in `PythLazerLib.sol` share the same pattern:

```solidity
function _readBytes8(bytes memory data, uint16 pos) private pure returns (uint64 value) {
    assembly {
        let word := mload(add(add(data, 0x20), pos))
        value := shr(192, word)
    }
}
```

There is no `require(pos + N <= data.length)` guard before the `mload`. [1](#0-0) 

These helpers are called by every public parsing function — `parsePayloadHeader`, `parseFeedHeader`, `parseFeedProperty`, and all `parseFeedValue*` variants — none of which add their own bounds checks either. [2](#0-1) 

`parseUpdateFromPayload` is also `public` and calls all of the above. Its only integrity check is a trailing `require(pos == payload.length, "Payload has extra unknown bytes")` at the end. [3](#0-2) 

The `pos` cursor is typed as `uint16` (max 65535). With `feedsLen` up to `uint8(255)` and `numProperties` up to `uint8(255)`, the maximum total byte consumption is `14 + 255 × (5 + 255 × 9) ≈ 586 000`, which overflows `uint16` multiple times. When `pos` wraps, the trailing equality check `pos == payload.length` can be satisfied by a short crafted payload, bypassing the only guard entirely.

`verifyUpdate` in `PythLazer.sol` only validates the ECDSA signature and that `update.length >= 71 + payload_len`; it does not validate the internal structure of the payload. [4](#0-3) 

---

### Impact Explanation

1. **Direct calls to public parsing functions**: Any unprivileged caller can invoke `parseUpdateFromPayload` (or any individual `public` parse function) with a crafted short payload. The `_readBytes*` helpers silently return 0 (or partial data) for out-of-bounds positions instead of reverting. A consumer contract that calls these functions directly — without first calling `verifyUpdate` — will receive silently incorrect price data (zeros or garbage) and may act on it.

2. **`uint16` overflow bypass**: A crafted payload with `feedsLen = 255` and `numProperties = 255` causes `pos` to overflow `uint16` and wrap to a small value. If the attacker sizes the payload so that `payload.length` equals the wrapped `pos`, the trailing `require(pos == payload.length)` passes, and `parseUpdateFromPayload` returns a fully "parsed" struct populated with zeros from OOB reads — with no revert.

3. **Downstream price corruption**: Consumer contracts that use the returned `Update` struct to make financial decisions (e.g., checking `getPrice`, `getConfidence`) would receive zero-valued prices, potentially triggering incorrect logic, free liquidations, or mispriced trades.

---

### Likelihood Explanation

The parsing functions are `public` and documented as the intended API for consumers. The Pyth Lazer developer documentation explicitly shows consumers calling `parsePayloadHeader` and `parseFeedHeader` directly in a loop. A consumer that omits the `verifyUpdate` step, or that calls the parse functions on a payload that was truncated or malformed, is immediately exposed. The `uint16` overflow path requires a crafted payload but is fully attacker-controlled with no privileged access needed.

---

### Recommendation

Add explicit bounds checks to every `_readBytes*` helper before the `mload`:

```solidity
function _readBytes8(bytes memory data, uint16 pos) private pure returns (uint64 value) {
    require(uint256(pos) + 8 <= data.length, "out of bounds");
    assembly {
        let word := mload(add(add(data, 0x20), pos))
        value := shr(192, word)
    }
}
```

Apply the same pattern to `_readBytes1` (`+1`), `_readBytes2` (`+2`), and `_readBytes4` (`+4`).

Additionally, change the `pos` cursor type from `uint16` to `uint256` (or at minimum `uint32`) to eliminate the overflow-based bypass of the trailing length check.

---

### Proof of Concept

Deploy `PythLazerLib` and call `parseUpdateFromPayload` with a payload that declares `feedsLen = 1`, `numProperties = 1`, `propertyId = 0` (Price), but provides no bytes for the price value:

```solidity
// Payload: magic(4) + timestamp(8) + channel(1) + feedsLen=1(1)
//        + feedId(4) + numProperties=1(1) + propertyId=0(1)
// Total = 20 bytes; price value (8 bytes) is missing.
bytes memory shortPayload = abi.encodePacked(
    uint32(2479346549),   // FORMAT_MAGIC
    uint64(1700000000),   // timestamp
    uint8(0),             // channel = RealTime
    uint8(1),             // feedsLen = 1
    uint32(1),            // feedId = 1
    uint8(1),             // numProperties = 1
    uint8(0)              // propertyId = Price (0)
    // price value (8 bytes) intentionally omitted
);
// pos after parsing header = 14, after feed header = 19, after property = 20
// _readBytes8(shortPayload, 20) reads mload at offset 20 beyond the 20-byte array
// → reads from free memory → returns 0
// feed._price = 0, triState = ApplicableButMissing
// Final check: pos (28) != payload.length (20) → reverts here.
// BUT: with feedsLen=255 and numProperties=255, pos wraps via uint16 overflow
// and the final check can be bypassed with a payload.length matching the wrapped value.
PythLazerStructs.Update memory u = PythLazerLib.parseUpdateFromPayload(shortPayload);
// Without overflow: reverts. With overflow crafting: returns zero-filled price data silently.
```

The root cause — `_readBytes*` performing unchecked assembly reads — is identical in structure to the Basin `getBytes32FromBytes()` off-by-one, and is reachable by any unprivileged caller of the `public` parsing API. [1](#0-0)

### Citations

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L66-108)
```text
    function _readBytes1(
        bytes memory data,
        uint16 pos
    ) private pure returns (uint8 value) {
        assembly {
            let word := mload(add(add(data, 0x20), pos))
            // Read first byte (most significant byte in the word)
            value := shr(248, word)
        }
    }

    function _readBytes2(
        bytes memory data,
        uint16 pos
    ) private pure returns (uint16 value) {
        assembly {
            let word := mload(add(add(data, 0x20), pos))
            // Read first 2 bytes (most significant bytes in the word)
            value := shr(240, word)
        }
    }

    function _readBytes4(
        bytes memory data,
        uint16 pos
    ) private pure returns (uint32 value) {
        assembly {
            let word := mload(add(add(data, 0x20), pos))
            // Read first 4 bytes (most significant bytes in the word)
            value := shr(224, word)
        }
    }

    function _readBytes8(
        bytes memory data,
        uint16 pos
    ) private pure returns (uint64 value) {
        assembly {
            let word := mload(add(add(data, 0x20), pos))
            // Read first 8 bytes (most significant bytes in the word)
            value := shr(192, word)
        }
    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L110-211)
```text
    function parsePayloadHeader(
        bytes memory update
    )
        public
        pure
        returns (
            uint64 timestamp,
            PythLazerStructs.Channel channel,
            uint8 feedsLen,
            uint16 pos
        )
    {
        uint32 FORMAT_MAGIC = 2479346549;

        pos = 0;
        uint32 magic = _readBytes4(update, pos);
        pos += 4;
        if (magic != FORMAT_MAGIC) {
            revert("invalid magic");
        }
        timestamp = _readBytes8(update, pos);
        pos += 8;
        channel = PythLazerStructs.Channel(_readBytes1(update, pos));
        pos += 1;
        feedsLen = uint8(_readBytes1(update, pos));
        pos += 1;
    }

    function parseFeedHeader(
        bytes memory update,
        uint16 pos
    )
        public
        pure
        returns (uint32 feed_id, uint8 num_properties, uint16 new_pos)
    {
        feed_id = _readBytes4(update, pos);
        pos += 4;
        num_properties = uint8(_readBytes1(update, pos));
        pos += 1;
        new_pos = pos;
    }

    function parseFeedProperty(
        bytes memory update,
        uint16 pos
    )
        public
        pure
        returns (PythLazerStructs.PriceFeedProperty property, uint16 new_pos)
    {
        uint8 propertyId = _readBytes1(update, pos);
        require(propertyId <= 12, "Unknown property");
        property = PythLazerStructs.PriceFeedProperty(propertyId);
        pos += 1;
        new_pos = pos;
    }

    function parseFeedValueUint64(
        bytes memory update,
        uint16 pos
    ) internal pure returns (uint64 value, uint16 new_pos) {
        value = _readBytes8(update, pos);
        pos += 8;
        new_pos = pos;
    }

    function parseFeedValueInt64(
        bytes memory update,
        uint16 pos
    ) internal pure returns (int64 value, uint16 new_pos) {
        value = int64(_readBytes8(update, pos));
        pos += 8;
        new_pos = pos;
    }

    function parseFeedValueUint16(
        bytes memory update,
        uint16 pos
    ) internal pure returns (uint16 value, uint16 new_pos) {
        value = _readBytes2(update, pos);
        pos += 2;
        new_pos = pos;
    }

    function parseFeedValueInt16(
        bytes memory update,
        uint16 pos
    ) internal pure returns (int16 value, uint16 new_pos) {
        value = int16(_readBytes2(update, pos));
        pos += 2;
        new_pos = pos;
    }

    function parseFeedValueUint8(
        bytes memory update,
        uint16 pos
    ) internal pure returns (uint8 value, uint16 new_pos) {
        value = _readBytes1(update, pos);
        pos += 1;
        new_pos = pos;
    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L217-562)
```text
    function parseUpdateFromPayload(
        bytes memory payload
    ) public pure returns (PythLazerStructs.Update memory update) {
        // Parse payload header
        uint16 pos;
        uint8 feedsLen;
        (update.timestamp, update.channel, feedsLen, pos) = parsePayloadHeader(
            payload
        );

        // Initialize feeds array
        update.feeds = new PythLazerStructs.Feed[](feedsLen);

        // Parse each feed
        for (uint8 i = 0; i < feedsLen; i++) {
            PythLazerStructs.Feed memory feed;

            // Parse feed header (feed ID and number of properties)
            uint32 feedId;
            uint8 numProperties;

            (feedId, numProperties, pos) = parseFeedHeader(payload, pos);

            // Initialize feed
            feed.feedId = feedId;
            feed.triStateMap = 0;

            // Parse each property
            for (uint8 j = 0; j < numProperties; j++) {
                // Read property ID
                PythLazerStructs.PriceFeedProperty property;
                (property, pos) = parseFeedProperty(payload, pos);

                // Parse value and set tri-state based on property type
                // Price Property
                if (property == PythLazerStructs.PriceFeedProperty.Price) {
                    (feed._price, pos) = parseFeedValueInt64(payload, pos);
                    if (feed._price != 0) {
                        _setPresent(
                            feed,
                            uint8(PythLazerStructs.PriceFeedProperty.Price)
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(PythLazerStructs.PriceFeedProperty.Price)
                        );
                    }

                    // Best Bid Price Property
                } else if (
                    property == PythLazerStructs.PriceFeedProperty.BestBidPrice
                ) {
                    (feed._bestBidPrice, pos) = parseFeedValueInt64(
                        payload,
                        pos
                    );
                    if (feed._bestBidPrice != 0) {
                        _setPresent(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.BestBidPrice
                            )
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.BestBidPrice
                            )
                        );
                    }

                    // Best Ask Price Property
                } else if (
                    property == PythLazerStructs.PriceFeedProperty.BestAskPrice
                ) {
                    (feed._bestAskPrice, pos) = parseFeedValueInt64(
                        payload,
                        pos
                    );
                    if (feed._bestAskPrice != 0) {
                        _setPresent(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.BestAskPrice
                            )
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.BestAskPrice
                            )
                        );
                    }

                    // Publisher Count Property
                } else if (
                    property ==
                    PythLazerStructs.PriceFeedProperty.PublisherCount
                ) {
                    (feed._publisherCount, pos) = parseFeedValueUint16(
                        payload,
                        pos
                    );
                    if (feed._publisherCount != 0) {
                        _setPresent(
                            feed,
                            uint8(
                                PythLazerStructs
                                    .PriceFeedProperty
                                    .PublisherCount
                            )
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(
                                PythLazerStructs
                                    .PriceFeedProperty
                                    .PublisherCount
                            )
                        );
                    }

                    // Exponent Property
                } else if (
                    property == PythLazerStructs.PriceFeedProperty.Exponent
                ) {
                    (feed._exponent, pos) = parseFeedValueInt16(payload, pos);
                    _setPresent(
                        feed,
                        uint8(PythLazerStructs.PriceFeedProperty.Exponent)
                    );

                    // Confidence Property
                } else if (
                    property == PythLazerStructs.PriceFeedProperty.Confidence
                ) {
                    (feed._confidence, pos) = parseFeedValueUint64(
                        payload,
                        pos
                    );
                    if (feed._confidence != 0) {
                        _setPresent(
                            feed,
                            uint8(PythLazerStructs.PriceFeedProperty.Confidence)
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(PythLazerStructs.PriceFeedProperty.Confidence)
                        );
                    }

                    // Funding Rate Property
                } else if (
                    property == PythLazerStructs.PriceFeedProperty.FundingRate
                ) {
                    uint8 exists;
                    (exists, pos) = parseFeedValueUint8(payload, pos);
                    if (exists != 0) {
                        (feed._fundingRate, pos) = parseFeedValueInt64(
                            payload,
                            pos
                        );
                        _setPresent(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.FundingRate
                            )
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.FundingRate
                            )
                        );
                    }

                    // Funding Timestamp Property
                } else if (
                    property ==
                    PythLazerStructs.PriceFeedProperty.FundingTimestamp
                ) {
                    uint8 exists;
                    (exists, pos) = parseFeedValueUint8(payload, pos);
                    if (exists != 0) {
                        (feed._fundingTimestamp, pos) = parseFeedValueUint64(
                            payload,
                            pos
                        );
                        _setPresent(
                            feed,
                            uint8(
                                PythLazerStructs
                                    .PriceFeedProperty
                                    .FundingTimestamp
                            )
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(
                                PythLazerStructs
                                    .PriceFeedProperty
                                    .FundingTimestamp
                            )
                        );
                    }

                    // Funding Rate Interval Property
                } else if (
                    property ==
                    PythLazerStructs.PriceFeedProperty.FundingRateInterval
                ) {
                    uint8 exists;
                    (exists, pos) = parseFeedValueUint8(payload, pos);
                    if (exists != 0) {
                        (feed._fundingRateInterval, pos) = parseFeedValueUint64(
                            payload,
                            pos
                        );
                        _setPresent(
                            feed,
                            uint8(
                                PythLazerStructs
                                    .PriceFeedProperty
                                    .FundingRateInterval
                            )
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(
                                PythLazerStructs
                                    .PriceFeedProperty
                                    .FundingRateInterval
                            )
                        );
                    }

                    // Market Session Property
                } else if (
                    property == PythLazerStructs.PriceFeedProperty.MarketSession
                ) {
                    int16 marketSessionValue;
                    (marketSessionValue, pos) = parseFeedValueInt16(
                        payload,
                        pos
                    );
                    require(
                        marketSessionValue >= 0 && marketSessionValue <= 4,
                        "Invalid market session value"
                    );
                    feed._marketSession = PythLazerStructs.MarketSession(
                        uint8(uint16(marketSessionValue))
                    );
                    _setPresent(
                        feed,
                        uint8(PythLazerStructs.PriceFeedProperty.MarketSession)
                    );
                    // EMA Price Property
                } else if (
                    property == PythLazerStructs.PriceFeedProperty.EmaPrice
                ) {
                    (feed._emaPrice, pos) = parseFeedValueInt64(payload, pos);
                    if (feed._emaPrice != 0) {
                        _setPresent(
                            feed,
                            uint8(PythLazerStructs.PriceFeedProperty.EmaPrice)
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(PythLazerStructs.PriceFeedProperty.EmaPrice)
                        );
                    }
                    // EMA Confidence Property
                } else if (
                    property == PythLazerStructs.PriceFeedProperty.EmaConfidence
                ) {
                    (feed._emaConfidence, pos) = parseFeedValueUint64(
                        payload,
                        pos
                    );
                    if (feed._emaConfidence != 0) {
                        _setPresent(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.EmaConfidence
                            )
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(
                                PythLazerStructs.PriceFeedProperty.EmaConfidence
                            )
                        );
                    }
                    // Feed Update Timestamp Property
                } else if (
                    property ==
                    PythLazerStructs.PriceFeedProperty.FeedUpdateTimestamp
                ) {
                    uint8 exists;
                    (exists, pos) = parseFeedValueUint8(payload, pos);
                    if (exists != 0) {
                        (feed._feedUpdateTimestamp, pos) = parseFeedValueUint64(
                            payload,
                            pos
                        );
                        _setPresent(
                            feed,
                            uint8(
                                PythLazerStructs
                                    .PriceFeedProperty
                                    .FeedUpdateTimestamp
                            )
                        );
                    } else {
                        _setApplicableButMissing(
                            feed,
                            uint8(
                                PythLazerStructs
                                    .PriceFeedProperty
                                    .FeedUpdateTimestamp
                            )
                        );
                    }
                } else {
                    // This should never happen due to validation in parseFeedProperty
                    revert("Unexpected property");
                }
            }

            // Store feed in update
            update.feeds[i] = feed;
        }

        // Ensure we consumed all bytes
        require(pos == payload.length, "Payload has extra unknown bytes");
    }
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L79-106)
```text
        if (update.length < 71) {
            revert("input too short");
        }
        uint32 EVM_FORMAT_MAGIC = 706910618;

        uint32 evm_magic = uint32(bytes4(update[0:4]));
        if (evm_magic != EVM_FORMAT_MAGIC) {
            revert("invalid evm magic");
        }
        uint16 payload_len = uint16(bytes2(update[69:71]));
        if (update.length < 71 + payload_len) {
            revert("input too short");
        }
        payload = update[71:71 + payload_len];
        bytes32 hash = keccak256(payload);
        (signer, , ) = ECDSA.tryRecover(
            hash,
            uint8(update[68]) + 27,
            bytes32(update[4:36]),
            bytes32(update[36:68])
        );
        if (signer == address(0)) {
            revert("invalid signature");
        }
        if (!isValidSigner(signer)) {
            revert("invalid signer");
        }
    }
```
