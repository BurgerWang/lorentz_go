# Limited external Classic comparison

Four unmodified public CSVs and `baselines_manifest.json` retrieved from
https://github.com/artificial-intelligence-edge/lorentzian-classification/tree/c5eea172b38d7e1585d6d12f4ae515bdf1f84951/tests/parity/baselines .

Run `GOCACHE=/tmp/lorentz-go-cache go run -buildvcs=false ./scripts/chartreference`.
Features/kernel compare every populated cell at absolute tolerance 1e-6;
integer predictions/directions and boolean entries compare from N-1-2000.
No synthetic reference output is used. Full CSV initialization and fixed chart
endpoint are retained; this does not test online causality or trade execution.

The preserved Classic implementation does **not** pass full parity. WT uses
first-finite-value EMA initialization; official baseline compatibility needs
SMA-seeded WT EMAs (index31 rather than4). Their early extremes persist in
running normalization. The Pine library calls builtin ta.ema without exposing
intermediate state, so this comparison alone does not establish a Pine bug.
Do not alter old EMA/normalization or rename old candidates to match the export.

H1 has no feature targets and has kernel values on the first26 rows despite
being a cropped history; the fixed KernelFunctions2 window is unavailable there.
TASTYFX ADX also differs; the official implementation quantizes price differences
using export precision. Exit markers are all hidden, and no backtest-stream,
funding or execution target is exported. The result records mismatches honestly.
