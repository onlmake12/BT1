The code at line 197 is unambiguous: [1](#0-0) 

```rust
let base64_config =
    base64::engine::GeneralPurposeConfig::new().with_decode_allow_trailing_bits(true);
let base64_engine =
    base64::engine::GeneralPurpose::new(&base64::alphabet::STANDARD, base64_config);
let spec_content = base64_engine.encode(encoded_content.trim());  // BUG: encode instead of decode
fs::write(target_file, spec_content)?;
```

The `base64_config` is constructed with `with_decode_allow_trailing_bits(true)` — a decode-only option — confirming the intent was to **decode** stdin. But `.encode()` is called instead, double-encoding the input and writing a base64 blob as the spec file.

Then at line 219: [2](#0-1) 

`AppConfig::load_for_subcommand` attempts to parse that blob as TOML, which fails. The `?` propagates the error and the process exits with `ExitCode::Failure`.

---

### Title
`ckb init --import-spec -` encodes stdin instead of decoding it, crashing on TOML parse — (`ckb-bin/src/subcommand/init.rs`)

### Summary
`init` with `--import-spec -` reads base64-encoded spec from stdin but calls `.encode()` instead of `.decode()`, writing a double-base64-encoded blob to the spec file. The immediately following `AppConfig::load_for_subcommand` call fails to parse it as TOML and the process crashes with a non-zero exit.

### Finding Description
At line 197, `base64_engine.encode(encoded_content.trim())` is called. The surrounding code (the `with_decode_allow_trailing_bits` config, the variable name `encoded_content`) makes clear the intent was `base64_engine.decode(...)`. The written file is not valid TOML; it is a base64 string of a base64 string. The `?` at line 219 propagates the resulting parse error, crashing the `init` subcommand.

### Impact Explanation
Any unprivileged local user running `ckb init --import-spec -` with a valid base64-encoded spec on stdin will always get a crash. The spec file is also left in a corrupt state. Scope: **Any local command line crash (0–500 pts)**.

### Likelihood Explanation
The flag `--import-spec -` is a documented stdin import path. Any operator following the documented workflow hits this unconditionally — 100% reproduction rate.

### Recommendation
Replace line 197:
```rust
// wrong
let spec_content = base64_engine.encode(encoded_content.trim());
// correct
let spec_content = base64_engine.decode(encoded_content.trim())
    .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
```

### Proof of Concept
```bash
# Encode a valid dev spec as base64 and pipe it
BASE64_SPEC=$(base64 < specs/dev.toml)
echo "$BASE64_SPEC" | ckb init --import-spec - --chain dev --root-dir /tmp/ckb-test
# Expected: Genesis Hash printed
# Actual:   TOML parse error, non-zero exit; specs/dev.toml contains a base64 blob
```

### Citations

**File:** ckb-bin/src/subcommand/init.rs (L193-198)
```rust
            let base64_config =
                base64::engine::GeneralPurposeConfig::new().with_decode_allow_trailing_bits(true);
            let base64_engine =
                base64::engine::GeneralPurpose::new(&base64::alphabet::STANDARD, base64_config);
            let spec_content = base64_engine.encode(encoded_content.trim());
            fs::write(target_file, spec_content)?;
```

**File:** ckb-bin/src/subcommand/init.rs (L219-228)
```rust
    let genesis_hash = AppConfig::load_for_subcommand(args.root_dir, cli::CMD_INIT)?
        .chain_spec()?
        .build_genesis()
        .map_err(|err| {
            eprintln!(
                "Couldn't build the genesis block from the generated chain spec, since {err}"
            );
            ExitCode::Failure
        })?
        .hash();
```
