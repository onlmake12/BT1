### Title
`uint16` Overflow in Assembly-Based Parser Bypasses Payload Integrity Check — (`lazer/contracts/evm/src/PythLazerLib.sol`)

### Summary
`PythLazerLib.parseUpdateFromPayload` uses a `uint16 pos` cursor and four assembly-only byte-reading helpers (`_readBytes1/2/4/8`) that perform **no bounds checking**. A malicious Lazer updater can craft a payload whose feed/property counts cause `pos` to silently overflow `uint16`, wrapping back to a value that equals `payload.length`, thereby bypassing the terminal integrity check and causing the parser to accept garbage price data derived from out-of-bounds memory reads.

---

### Finding Description

`PythLazerLib` implements its own byte-reading primitives entirely in inline assembly:

```solidity
function _readBytes8(bytes memory data, uint16 pos) private pure returns (uint64 value) {
    assembly {
        let word := mload(add(add(data, 0x20), pos))
        value := shr(192, word)
    }
}
```

None of `_readBytes1`, `_readBytes2`, `_readBytes4`, or `_readBytes8` check that `pos + size <= data.length` before issuing `mload`. [1](#0-0) 

The main parsing entry point declares the cursor as `uint16`:

```solidity
function parseUpdateFromPayload(bytes memory payload)
    public pure returns (PythLazerStructs.Update memory update)
{
    uint16 pos;
    ...
    for (uint8 i = 0; i < feedsLen; i++) {
        ...
        for (uint8 j = 0; j < numProperties; j++) {
            ...
            pos += N;   // increments of 1, 2, 8 bytes
        }
    }
    require(pos == payload.length, "Payload has extra unknown bytes");
}
``` [2](#0-1) 

`feedsLen` is a `uint8` (0–255) and `numProperties` is also a `uint8` (0–255), both read directly from the attacker-supplied payload bytes. [3](#0-2) 

The maximum cumulative increment before `uint16` wraps is 65 535. With `feedsLen = 255` and `numProperties = 255`, each property consuming 9 bytes on average, the total increment is `255 × (5 + 255 × 9) ≈ 586 500` — far beyond `uint16` max. Solidity's `unchecked`-style arithmetic on `uint16` wraps silently. The attacker can tune `feedsLen`/`numProperties` so that `pos` wraps to exactly `payload.length` (a small number), satisfying the terminal `require`. [4](#0-3) 

During the overflow region, every `mload` in the assembly helpers reads beyond the allocated array. In the EVM, unallocated memory returns zero, so price fields are silently set to `0`. The `_setApplicableButMissing` / `_setPresent` tri-state logic treats `0` as "missing" for most fields, but the `Exponent` property unconditionally calls `_setPresent` regardless of value, meaning a zero exponent is accepted as valid. [5](#0-4) 

---

### Impact Explanation

A malicious Lazer updater can submit a signed payload that:
1. Passes signature verification (the updater holds a valid key).
2. Causes `pos` to overflow `uint16` and wrap to `payload.length`.
3. Passes the terminal `require(pos == payload.length)`.
4. Populates on-chain price feeds with attacker-chosen zero/garbage values.

Any on-chain consumer (DeFi protocol, liquidation engine, etc.) reading Lazer prices after such an update receives corrupted data, enabling price manipulation, incorrect liquidations, or protocol insolvency.

---

### Likelihood Explanation

The Pyth Immunefi scope explicitly lists "Lazer updater" as an in-scope attacker role. A compromised or malicious Lazer updater key is the only prerequisite. The arithmetic overflow is deterministic and requires no brute force — the attacker simply tunes `feedsLen` and `numProperties` in the crafted payload.

---

### Recommendation

1. Change `uint16 pos` to `uint256 pos` in `parseUpdateFromPayload` to eliminate overflow. [6](#0-5) 
2. Add explicit bounds checks to every `_readBytes*` helper before the `mload`, or replace the assembly entirely with safe Solidity slice operations (analogous to the `BytesLib` safe variants that include `require(_bytes.length >= _start + N)` guards). [1](#0-0) 
3. Consider replacing the custom assembly helpers with `abi.decode` or the checked `BytesLib` variants already present in the repository. [7](#0-6) 

---

### Proof of Concept

```
payload = [
  magic (4 bytes),
  timestamp (8 bytes),
  channel (1 byte),
  feedsLen = 200 (1 byte),
  // 200 feed blocks, each with numProperties = 50,
  // each property = Price (1 byte ID + 8 bytes value)
  // total increments = 14 (header) + 200*(5 + 50*9) = 14 + 200*455 = 91,014
  // 91,014 mod 65,536 = 25,478
  // set actual payload.length = 25,478
  // pos wraps to 25,478 == payload.length → require passes
  // all price reads during overflow region return 0 from unallocated EVM memory
]
```

The attacker signs this payload with their Lazer updater key and submits it. `parseUpdateFromPayload` returns an `Update` struct with all price fields zeroed and `Exponent` marked `Present` with value `0`, which downstream consumers treat as a valid price update.

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

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L130-150)
```text
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

**File:** target_chains/ethereum/contracts/contracts/libraries/external/BytesLib.sol (L323-335)
```text
    function toUint8(
        bytes memory _bytes,
        uint256 _start
    ) internal pure returns (uint8) {
        require(_bytes.length >= _start + 1, "toUint8_outOfBounds");
        uint8 tempUint;

        assembly {
            tempUint := mload(add(add(_bytes, 0x1), _start))
        }

        return tempUint;
    }
```
