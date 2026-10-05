# German BESS Multi-Market MILP

Daily mixed-integer linear optimization of a battery participating in German energy and ancillary-service markets. The default runnable case is **7 MW / 16.7 MWh**.

## Repository contents

| File | Purpose |
|---|---|
| `main.py` | Portable command-line runner; carries terminal SOC into the next day |
| `Optimizer.py` | PuLP MILP formulation and CBC solver |
| `BESS_MKT_Config.py` | Physical parameters, reserve assumptions and strategy profiles |
| `In_And_Out.py` | Excel ingestion, missing-value handling and result export |
| `german_stand_input.xlsx` | German market-price input workbook |
| `methodology.md` | Implementation notes and English mathematical formulation (Section 6) |
| `requirements.txt` | Python dependencies |

The original analysis folder contains other projects, historical results and reference documents. This repository includes the files required to run and understand the German model.

## Installation

Python 3.10 or newer is required. From the repository directory:

```sh
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate
python -m pip install -r requirements.txt
```

PuLP invokes CBC through `PULP_CBC_CMD`; a compatible CBC executable must be available in the PuLP installation.

## Run

One-day check of all three strategies:

```sh
python main.py --days 1
```

Full available period, default 7 MW / 16.7 MWh:

```sh
python main.py
```

Custom project and selected strategy:

```sh
python main.py --input german_stand_input.xlsx --power-mw 7 --energy-mwh 16.7 --strategies joint --output-dir outputs
```

Results are saved as timestamped Excel files in `outputs/`, with one detail sheet per strategy and a daily `summary`. The runner does not write imputed data back to the source workbook. Non-optimal solver results stop the run before SOC is carried forward.

## Strategies and model logic

| Strategy | Markets | Terminal SOC |
|---|---|---|
| `joint` | FCR + aFRR UP/DN + IDM | 40–60% |
| `da_only` | DA arbitrage | 10–90% |
| `fcr_only` | FCR; charging/discharging for SOC management | 35–65% |

The model maximizes energy-trading revenue, reserve-capacity revenue and expected aFRR activation revenue, less charging costs and linear degradation costs. Charging/discharging decisions use 15-minute intervals; reserve capacities are integer MW decisions held for each four-hour block. Power competition, SOC dynamics, charging/discharging exclusivity and reserve energy headroom constrain the solution. Each day is optimized using that day's known prices and the previous day's terminal SOC. This is sequential daily optimization, not a single annual optimum or an intraday forecast-updating algorithm.

`BESS_MKT_Config()` retains its original 10 MW / 10 MWh defaults; `main.py` explicitly overrides these with CLI settings, defaulting to 7 MW / 16.7 MWh.

## Input workbook

The `Input` sheet is read positionally. Preserve the following column order:

| Column | Data | Unit |
|---|---|---|
| A | Date | Calendar date |
| B | Minute/time | Delivery time |
| C | Hour | 0–23 |
| D | DA price | EUR/MWh |
| E | FCR capacity price | EUR/MW per four-hour product, as modeled |
| F | aFRR UP capacity price | EUR/(MW·h) |
| G | aFRR DN capacity price | EUR/(MW·h) |
| H | aFRR UP energy price | EUR/MWh |
| I | aFRR DN energy price | EUR/MWh |
| J | IDM price | EUR/MWh; optional |

The input layer aggregates duplicate date/slot entries, expands hourly data if required and prepares 96 intervals per day. It interpolates energy prices and fills capacity prices within blocks. Missing IDM prices are simulated from DA using a seeded normal draw with standard deviation `max(1, 0.05 * abs(DA))`. The supplied workbook's empty IDM column therefore implies simulated IDM trading prices.

## Assumptions and interpretation

- Charging/discharging efficiencies: 93% each; physical SOC range: 5–95%; initial SOC: 50%.
- FCR marketable power: 80% of rated power; reserve headroom durations: 0.83 h FCR and 1 h aFRR per direction.
- Capacity acceptance probabilities: 100%; aFRR expected activation rates: `2/24` per direction.
- Degradation: EUR 1 per MWh of modeled charging, discharging and expected activation throughput.
- FCR activation energy is not modeled explicitly. `fcr_only` SOC-management energy has no energy-market cost or revenue term, which limits interpretation of that comparison.
- Prices are exogenous; bidding curves, forecast errors, stochastic activation, detailed AGC tracking and penalties are not modeled.
- CAPEX, fixed OPEX, trader/BSP fees, taxes and financing are excluded.
- The runner fixes every day to 96 intervals; daylight-saving days are not represented with variable-length local-time horizons.

The historical 7 MW / 16.7 MWh analysis reported approximately EUR 2.05 million of annual joint-strategy operating revenue. Historical output workbooks are not included. Reruns may differ with changed input data, code, solver versions or parameters; this figure is a reference rather than a guaranteed reproduction target.

See [methodology.md](methodology.md#6-milp-mathematical-formulation-and-current-model-assumptions-english) for equations and assumptions.
