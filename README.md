# solana-dex-program-dumps

Daily snapshots of the on-chain program binaries (`.so`) of the major Solana DEXs, collected
from mainnet with the official Solana CLI by GitHub Actions. Every deployed version that was
observed is kept, named by its `last_deployed_slot`, for reverse engineering, auditing and
version checks.

- Binaries: one [GitHub release](https://github.com/cpkt9762/solana-dex-program-dumps/releases) per program version, tag `<name>-<last_deployed_slot>`
- Metadata of the latest version: `dumps/<name>/metadata.json`
- Metadata of superseded versions: `archives/<name>/<last_deployed_slot>/metadata.json`
- Every version of every program in one file: [`index.json`](index.json)
- Programs tracked: see [Tracked programs](#tracked-programs) and [`registry.json`](registry.json)

## Where things are

Git only holds text. The binaries are release assets, so the repository stays small, downloads
have no quota, and a single version can be removed without rewriting git history.

```text
registry.json                               programs to track (name, program_id, type, description)
index.json                                  all known versions per program, newest first
dumps/<name>/metadata.json                  latest version
archives/<name>/<slot>/metadata.json        superseded versions
scripts/collect.py                          collector (Python 3.11+, standard library only)
tests/test_collect.py                       unit tests (no network needed)
.github/workflows/collect.yml               daily job: collect, publish releases, commit metadata
release <name>-<slot>  ->  <name>-<slot>.so the binary of that version
```

## Downloading binaries

```bash
# One version, from its metadata
curl -LO "$(jq -r .download_url dumps/pump-fun/metadata.json)"

# With the GitHub CLI
gh release download pump-fun-458921440 --repo cpkt9762/solana-dex-program-dumps

# The latest version of every program
jq -r '.programs[].versions[0].download_url // empty' index.json | xargs -n1 curl -LO
```

## How it works

For every program in `registry.json`, `scripts/collect.py` does the following:

1. `solana program show` reads `last_deployed_slot`, the upgrade authority and the ProgramData address.
2. It compares the slot with `dumps/<name>/metadata.json`. If the slot is unchanged, the program is
   skipped without downloading anything.
3. If the slot changed, or no slot exists (see below), `solana program dump` downloads the binary,
   which is then compared by sha256.
4. On a real change the binary is published as release `<name>-<slot>` with asset `<name>-<slot>.so`.
   Only after that succeeds, the old `metadata.json` is moved to `archives/<name>/<old slot>/` and
   the new one is written. `index.json` is regenerated at the end of every run.

Change detection uses `last_deployed_slot` first and sha256 second. Programs owned by the
non-upgradeable loader (for example `orca-v1`) report no slot, so they are compared by sha256 only and
named `sha256-<first 12 hex chars>` instead of a slot (release `orca-v1-sha256-4f316b5d1eb4`).

A failure for one program (RPC error, timeout, malformed binary, GitHub API error) is logged and
retried with backoff and never stops the run. The script exits with 1 if any program failed, so the
workflow turns red while still committing the programs that did update. If a program is redeployed
while it is being downloaded, it is downloaded again, so the slot always matches the bytes.
Publishing is idempotent: if a run created a release but failed before pushing the metadata, the
next run finds the release and reuses it.

### Version rules

| Where | What |
|---|---|
| `dumps/<name>/metadata.json` | always the newest version seen |
| `archives/<name>/<slot>/metadata.json` | the version that was live at `last_deployed_slot = <slot>` before it was replaced |
| release `<name>-<slot>` | the binary of that version, kept after it is superseded |
| `previous_slot` | slot of the version that this one replaced (null for the first one) |
| `collected_at` | UTC time at which this version was first observed, not the deploy time |

The deploy time itself is not stored: it can be looked up from the slot (`solana block-time <slot>`),
as long as the RPC node still has that block.

### `metadata.json`

```json
{
  "name": "raydium-amm-v4",
  "program_id": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
  "loader": "BPFLoaderUpgradeab1e11111111111111111111111",
  "program_data_address": "A7ZG7ByDi8DpzT9Ab7CiXhvgYTJQmaDPJkMDoPitaCQV",
  "sha256": "3849aece6fee86df0090d2824396f29220b5948da34d2ab5c7167655684a6467",
  "size": 308616,
  "onchain_sha256": "3bcf0828cf38e5f703adbf31dc1bf81f0a1254dcb176ca950ae52231c20e6d14",
  "onchain_size": 1406384,
  "last_deployed_slot": 445763204,
  "upgrade_authority": "FytDrVzDybM1TwFQPGb8qaxZR7dBCzNeqT3vtQsceZQK",
  "collected_at": "2026-10-24T18:05:25Z",
  "previous_slot": null,
  "release_tag": "raydium-amm-v4-445763204",
  "download_url": "https://github.com/cpkt9762/solana-dex-program-dumps/releases/download/raydium-amm-v4-445763204/raydium-amm-v4-445763204.so"
}
```

`program_data_address`, `last_deployed_slot` and `upgrade_authority` are `null` for non-upgradeable
programs; `upgrade_authority` is also `null` once a program has been made immutable.

### Padding is stripped from the stored file

A ProgramData account is usually allocated larger than the program (Pump.fun: 10.5 MB on chain,
1.9 MB of real ELF), and the rest is zeros. The published `.so` is the on-chain data without that
padding: everything up to the last non-zero byte or the end of the ELF headers' extent (program
headers, section headers, segments, sections), whichever is larger. Only zero bytes are ever removed,
and the result is still a complete, valid ELF file. (Plain trailing-zero stripping is not enough:
the last section header usually ends in zero bytes.)

Both hashes are recorded so either form can be verified:

| Field | Covers |
|---|---|
| `sha256`, `size` | the published `.so` (padding stripped) |
| `onchain_sha256`, `onchain_size` | the raw bytes exactly as `solana program dump` writes them |

### Verifying a dump

```bash
solana program dump <program_id> /tmp/raw.so --url https://api.mainnet-beta.solana.com
sha256sum /tmp/raw.so            # equals onchain_sha256 while the program is unchanged
sha256sum <name>-<slot>.so       # equals sha256
```

## Run locally

Requirements: Python 3.11+ and the [Solana CLI from Anza](https://docs.anza.xyz/cli/install)
(`solana` and `solana-keygen` in `PATH`). No third-party Python packages are needed.

```bash
# Try it without touching the repository or GitHub: metadata and binaries go to ./dry-run
python3 scripts/collect.py --dry-run dry-run --only pump-fun phoenix

# Publish for real (needs the GitHub CLI logged in with write access to the repository)
python3 scripts/collect.py --release-repo cpkt9762/solana-dex-program-dumps

python3 -m unittest discover -s tests            # unit tests
```

`--dry-run DIR` copies the current metadata into `DIR` (which must be empty or not exist), runs the
full collection there and saves the binaries under `DIR/releases/<tag>/`. Other options:
`--verify-hash` (always download and compare sha256), `--retries N`, `--delay SECONDS` (pause between
programs, default 0.5), `--root PATH`, `-v`.

The Solana CLI refuses to run without a signer even for read-only commands, so the script creates a
throwaway keypair in a temporary directory for each run. It holds no funds, and nothing is ever
signed or sent.

## Configure the RPC endpoint

The collector reads the `RPC_URL` environment variable and falls back to the public
`https://api.mainnet-beta.solana.com`. The public endpoint is rate limited; for reliable daily runs
use your own provider.

- Locally: `RPC_URL=https://your-rpc.example python3 scripts/collect.py ...`
- GitHub Actions: add a repository secret named `SOLANA_RPC_URL`
  (Settings > Secrets and variables > Actions). If it is not set, the public RPC is used.
- Optional repository variable `SOLANA_VERSION` (for example `v3.1.14`) pins the Solana CLI release
  that the workflow installs; the default is `stable`.

The RPC URL is never logged, and URLs in CLI error output are redacted.

## Add a program

1. Append an entry to `registry.json`:

   ```json
   {"name": "my-dex", "program_id": "<base58 program id>", "type": "amm", "description": "My DEX"}
   ```

   `name` becomes a directory name and part of the release tags, so it must be lowercase letters,
   digits and single hyphens, and both `name` and `program_id` must be unique.
2. Try it: `python3 scripts/collect.py --dry-run dry-run --only my-dex`
3. Refresh the table below with `python3 scripts/collect.py --print-registry-table`.
4. Commit. The next scheduled run (or a manual run of the workflow) publishes it.

Do not rename an existing `name`: its history lives under that name.

## Remove a program or a version

For example after a takedown request:

```bash
gh release list --limit 1000 --json tagName --jq '.[].tagName' | grep '^<name>-'   # its releases
gh release delete <name>-<slot> --cleanup-tag --yes          # delete one version (binary and tag)
```

To stop tracking a program, also remove it from `registry.json` and delete `dumps/<name>/` and
`archives/<name>/` (or keep them as a record without binaries). None of this needs a history
rewrite, because git never contained the binaries.

## GitHub Actions

`.github/workflows/collect.yml` runs daily at 02:00 UTC and can be started manually from the
Actions tab (optionally for a subset of programs, or with sha256 verification). It installs the
Solana CLI, runs the unit tests, runs the collector (which creates the releases with the workflow's
`GITHUB_TOKEN`), then commits and pushes the metadata as `github-actions[bot]` with a message that
lists every program that changed. The workflow requests `contents: write`, which covers both pushes
and releases.

Things worth knowing:

- **Why releases and not Git LFS.** LFS downloads by anyone (clones, forks) count against the
  repository owner's quota, and once it is exhausted cloners only get pointer files. Release assets
  have no bandwidth or total size limit (each file must be under 2 GiB; the largest program here is
  about 2.6 MB), and git history only grows by small JSON files.
- **First run.** The first run publishes one release per program (about 52 MB in total). It takes a
  few minutes because programs are fetched one after another.
- **Idle repositories.** GitHub disables scheduled workflows after 60 days without repository
  activity. If nothing was committed for 45 days the job adds an empty keepalive commit.
- **No overlapping runs.** A concurrency group serialises runs.
- **Failures.** If some programs cannot be collected, the others are still committed and the job
  is marked as failed at the end.

## Tracked programs

Program IDs come from the [Jupiter program-id-to-label list](https://lite-api.jup.ag/swap/v1/program-id-to-label)
plus manual additions. Every ID below was dumped successfully from mainnet; names, types and
descriptions are best effort. The `type` column is a rough classification: `amm` constant
product or SPL-token-swap style, `clmm` / `dlmm` concentrated or bin liquidity, `clob` order book,
`stable-swap`, `pmm` oracle-priced or proprietary market makers (mostly closed source),
`bonding-curve` launchpads, `perp` perpetuals, `lst-swap` liquid staking token swaps.

| Name | Program ID | Type | Description |
|---|---|---|---|
| `raydium-amm-v4` | `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8` | amm | Raydium AMM v4, constant-product AMM |
| `raydium-cpmm` | `CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C` | amm | Raydium CPMM, constant-product market maker (new architecture) |
| `raydium-clmm` | `CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK` | clmm | Raydium CLMM, tick-based concentrated liquidity |
| `raydium-launchlab` | `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj` | bonding-curve | Raydium LaunchLab, token launchpad with bonding curve |
| `orca-whirlpool` | `whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc` | clmm | Orca Whirlpools, concentrated liquidity AMM |
| `meteora-dlmm` | `LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo` | dlmm | Meteora DLMM, bin-based dynamic liquidity market maker |
| `meteora-damm-v2` | `cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG` | amm | Meteora DAMM v2 (Dynamic AMM v2), constant product with optional price range and dynamic fees |
| `meteora-damm-v1` | `Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB` | amm | Meteora Dynamic AMM v1 (legacy) |
| `meteora-dbc` | `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN` | bonding-curve | Meteora Dynamic Bonding Curve |
| `phoenix` | `PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY` | clob | Phoenix, on-chain central limit order book |
| `openbook-v2` | `opnb2LAfJYbRMAHHvqjCwQxanZn7ReEHp1k81EohpZb` | clob | OpenBook v2, on-chain central limit order book |
| `manifest` | `MNFSTqtC93rEfYHB6hF82sKdZpUDFWkViLByLd1k1Ms` | clob | Manifest, on-chain order book |
| `serum-dex-v3` | `srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX` | clob | Serum DEX v3 (legacy order book, still referenced by Raydium AMM v4 pools) |
| `saber` | `SSwpkEEcbUqx4vtoEByFjSkhKdCT862DNVb52nZg1UZ` | stable-swap | Saber StableSwap |
| `saber-decimals` | `DecZY86MU5Gj7kppfUCEmd4LbXXuyZH1yHaP2NTqdiZB` | stable-swap | Saber Decimals, decimal-wrapper pools on top of StableSwap |
| `byreal` | `REALQqNEomY6cQGZJUGwywTBD2UmDT32rZcNnfxQ5N2` | clmm | Byreal CLMM (Raydium CLMM fork) |
| `crema` | `CLMM9tUoggJu2wagPkkqs9eFG4BWhVBZWkP1qv3Sp7tR` | clmm | Crema Finance CLMM |
| `invariant` | `HyaB3W9q6XdA5xwpU4XnSZV94htfmbmqJXZcEbRaJutt` | clmm | Invariant concentrated liquidity AMM |
| `saros-dlmm` | `1qbkdrr3z4ryLA7pZykqxvxWPoeifcVKo6ZG9CfkvVE` | dlmm | Saros DLMM, bin-based liquidity |
| `saros-amm` | `SSwapUtytfBdBn1b9NUGG6foMVPtcWgpRU32HToDUZr` | amm | Saros AMM (SPL token-swap fork) |
| `perena` | `NUMERUNsFCP3kuNmWZuXtm1AaQCPj9uw6Guv2Ekoi5P` | stable-swap | Perena Numeraire, stable-swap AMM |
| `metadao-futarchy` | `FUTARELBfJfQ8RDGhg1wdhddq1odMAJUePHFuBYfUxKq` | amm | MetaDAO futarchy, conditional-market AMM |
| `moonit` | `MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG` | bonding-curve | Moonit bonding-curve launchpad |
| `omnipair` | `omnixgS8fnqHfCcTGKWj6JtKjzpJZ1Y5y9pyFkQDkYE` | amm | Omnipair, constant-product AMM combined with lending |
| `sanctum-lst-swap` | `stkitrT1Uoy18Dk1fTrgPw8W6MVzoCfYoAFT4MLsmhq` | lst-swap | Sanctum LST-to-LST swap program |
| `sanctum-infinity` | `5ocnV1qiCgaQR8Jb8xWnVbApfaygJ8tNoZfgPwsgx9kx` | lst-swap | Sanctum Infinity, multi-LST liquidity pool |
| `pump-fun` | `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` | bonding-curve | Pump.fun bonding-curve launchpad |
| `pump-swap` | `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` | amm | PumpSwap, constant-product AMM for graduated Pump.fun tokens |
| `boop-fun` | `boop8hVGQGqehUK2iVEMEnMrL5RbjywRzHKBmBE7ry4` | bonding-curve | Boop.fun bonding-curve launchpad |
| `virtuals` | `5U3EU2ubXtK84QcRjWVmYt9RaDyA8gKxdUrPFXmZyaki` | bonding-curve | Virtuals buy/sell program |
| `lifinity-v2` | `2wT8Yq49kHgDzXuPxZSaeLaH1qbmGXtEyPy64bL7aD3c` | pmm | Lifinity v2, oracle-driven proactive market maker |
| `lifinity-v1` | `EewxydAPCCVuNEyrVN68PuSYdQ7wKn27V9Gjeoi8dy3S` | pmm | Lifinity v1 (legacy, superseded by v2) |
| `woofi` | `WooFif76YGRNjk1pA8wCsN67aQsD9f9iLsz4NcJ1AVb` | pmm | WOOFi, oracle-priced PMM |
| `humidifi` | `9H6tua7jkLhdm3w8BvgpTn5LZNU7g4ZynDmCiNN3q6Rp` | pmm | HumidiFi, closed-source oracle-priced prop AMM |
| `spl-token-swap` | `SwaPpA9LAaLfeLi3a68M4DjnLqgtticKg6CnyNwgAC8` | amm | SPL Token Swap reference program |
| `orca-v1` | `DjVE6JNiYqPL2QXyCUUh8rNjHrbz9hXHNYt99MQ59qw1` | amm | Orca v1 (SPL token-swap based) |
| `orca-v2` | `9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP` | amm | Orca v2 (SPL token-swap based) |
| `stepn` | `Dooar9JkhdZ7J3LHN3A7YCuoGRUggXhQaG4kijfLGU2j` | amm | STEPN swap (SPL token-swap fork) |
| `penguin` | `PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP` | amm | Penguin Finance swap (SPL token-swap fork) |
| `guacswap` | `Gswppe6ERWKpUTXvRPfXdzHhiCyJvLadVvXGfdpBqcE1` | amm | Guacswap (SPL token-swap fork) |
| `fluxbeam` | `FLUXubRmkEi2q6K3Y9kBPg9248ggaZVsoSFhtJHSrm1X` | amm | FluxBeam (SPL token-swap fork) |
| `dexlab` | `DSwpgjMvXhtGn6BsbqmacdBZyfLj6jSWf3HJpdJtmg6N` | amm | DexLab swap (SPL token-swap fork) |
| `aldrin-v1` | `AMM55ShdkoGRB5jVYPjWziwk8m5MpwyDgsMWHaMSQWH6` | amm | Aldrin AMM v1 (SPL token-swap fork) |
| `aldrin-v2` | `CURVGoZn8zycx6FXwwevgBTB2gVvdbGTEpvMJDbgs2t4` | amm | Aldrin AMM v2 (SPL token-swap fork) |
| `mercurial` | `MERLuDFBMmsHnsBPZw2sDQZHvXFMwp8EdjudcU2HKky` | stable-swap | Mercurial multi-token stable-swap (merged into Meteora) |
| `bonkswap` | `BSwp6bEBihVLdqJRKGgzjcGLHkcTuzmSo1TQkHepzH8p` | amm | BonkSwap (SPL token-swap fork) |
| `cropper` | `H8W3ctz92svYg6mkn1UtGfu2aQr2fnUFHM1RhScEtQDt` | amm | Cropper Finance (SPL token-swap fork) |
| `pancakeswap` | `HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq` | clmm | PancakeSwap on Solana (CLMM fork) |
| `goosefx-gamma` | `GAMMA7meSFWaBXF25oSUgmGRwaW6sCMFLmBNiMSdbHVT` | amm | GooseFX GAMMA AMM |
| `stabble-stable` | `swapNyd8XiQwJ6ianp9snpu4brUqFxadzvHebnAXjJZ` | stable-swap | Stabble Stable Swap |
| `stabble-weighted` | `swapFpHZwjELNnjvThjajtiVmkz3yPQEHjLtka2fwHW` | amm | Stabble Weighted Swap, Balancer-style weighted pools |
| `stabble-clmm` | `6dMXqGZ3ga2dikrYS9ovDXgHGh5RUsb2RTUj6hrQXhk6` | clmm | Stabble CLMM |
| `drift-v2` | `dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH` | perp | Drift v2, perpetuals and spot DEX with AMM-backed liquidity |
| `jupiter-perps` | `PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu` | perp | Jupiter Perpetuals, LP-pool backed perps |
| `solfi` | `SoLFiHG9TfgtdUXUjWAxi3LtvYuFyDLVhBWxdMZxyCe` | pmm | SolFi v1, proprietary oracle-priced AMM |
| `solfi-v2` | `SV2EYYJyRz2YhfXwXnhNAevDEui5Q6yrfyo13WtupPF` | pmm | SolFi v2, proprietary oracle-priced AMM |
| `tessera-v` | `TessVdML9pBGgG9yGks7o4HewRaXVAMuoVj4x83GLQH` | pmm | TesseraV, proprietary AMM |
| `goonfi` | `goonERTdGsjnkZqWuVjs73BZ3Pb9qoCUdBUL17BnS5j` | pmm | GoonFi v1, proprietary AMM |
| `goonfi-v2` | `goonuddtQRrWqqn5nFyczVKaie28f3kDkHWkHtURSLE` | pmm | GoonFi v2, proprietary AMM |
| `zerofi` | `ZERor4xhbUycZ6gb9ntrhqscUcZmAbQDjEAtCf4hbZY` | pmm | ZeroFi, proprietary AMM |
| `bisonfi` | `BiSoNHVpsVZW2F7rx2eQ59yQwKxzU5NvBcmKshCSUypi` | pmm | BisonFi, proprietary AMM |
| `obric-v2` | `obriQD1zbpyLz95G5n7nJe6a4DPjpFwa5XYPoNm113y` | pmm | Obric v2, proprietary AMM |
| `aquifer` | `AQU1FRd7papthgdrwPTTq5JacJh8YtwEXaBfKU3bTz45` | pmm | Aquifer, proprietary AMM |
| `alphaq` | `ALPHAQmeA7bjrVuccPsYPiCvsi428SNwte66Srvs4pHA` | pmm | AlphaQ, proprietary AMM |
| `quantum` | `QuaNtZsgYRe5Z9Bk4LZ4cTD9tbkVoyCNf1R2BN9bBDv` | pmm | Quantum, proprietary AMM |
| `scorch` | `ojh19ojaKduoJZuaJADhcVGp4xt1TcdAvZmpVsCorch` | pmm | Scorch, proprietary AMM |
| `obsidian` | `HBVw6bZtcCaezhcBrmfyXBSBRWCdv72271xQ4GPvms2z` | pmm | Obsidian, proprietary AMM |
| `heaven` | `HEAVENoP2qxoeuF8Dj2oT1GHEnu49U5mJYkdeC8BAX2o` | pmm | Heaven, proprietary AMM |
| `taurusfi` | `9VX8EKBg6vM6tA68xaDsPkbrx26XConZjkQmhVApUptc` | pmm | TaurusFi, proprietary AMM |
| `gavel` | `srAMMzfVHVAtgSJc8iH6CfKzuWuUTzLHVCE81QU1rgi` | pmm | Gavel, proprietary AMM |
| `whalestreet` | `FW6zUqn4iKRaeopwwhwsquTY6ABWLLgjxtrC3VPnaWBf` | pmm | WhaleStreet, proprietary AMM |
| `marco-polo` | `9tKE7Mbmj4mxDjWatikzGAtkoWosiiZX9y6J4Hfm2R8H` | pmm | Marco Polo, proprietary AMM |

## Notes and limitations

- The binaries are public on-chain data and are provided for research, auditing and version
  checking. Their copyright stays with the respective authors. If you are the author of a program and
  want it removed, open an issue.
- Nothing here is a statement about who controls a program; that is only what the chain says
  (`upgrade_authority`).
- Programs can be closed or become immutable; a program that disappears shows up as a failure in
  the logs, and its published versions stay available.
- Only programs owned by the upgradeable loader and the non-upgradeable `BPFLoader2` have been
  exercised so far. Programs owned by other loaders (for example loader v4) are untested.
- The collector sees one snapshot per day. If a program is upgraded twice between two runs, only the
  last deployment is recorded. History starts with the first run of this repository.
