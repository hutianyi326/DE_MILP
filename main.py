"""Run daily German BESS multi-market optimization."""
from argparse import ArgumentParser
from dataclasses import replace
from pathlib import Path

from BESS_MKT_Config import BESS_MKT_Config, STRATEGIES
from In_And_Out import read_file, print_result
from Optimizer import Optimizer


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path(__file__).parent / 'german_stand_input.xlsx')
    parser.add_argument('--power-mw', type=float, default=7.0)
    parser.add_argument('--energy-mwh', type=float, default=16.7)
    parser.add_argument('--strategies', nargs='+', choices=list(STRATEGIES), default=list(STRATEGIES))
    parser.add_argument('--days', type=int, default=0, help='First N days; 0 runs all available days')
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).parent / 'outputs')
    args = parser.parse_args()
    if args.power_mw <= 0 or args.energy_mwh <= 0 or args.days < 0:
        parser.error('Power and energy must be positive; days must be nonnegative')
    config = replace(BESS_MKT_Config(), P_MAX=args.power_mw, E_MAX=args.energy_mwh)
    data = read_file(str(args.input.resolve()), config=config, write_back_sheet=False)
    dates = data['available_dates']
    if args.days:
        dates = dates[:args.days]
    if not dates:
        raise ValueError('No available input dates')
    optimizer = Optimizer(config)
    results = {}
    for strategy in args.strategies:
        results[strategy] = {}
        soc = config.E_MAX * config.SOC_INIT
        for dt in dates:
            inputs = data['days'][dt]
            result = optimizer.build_and_solve(inputs, strategy=strategy, soc_init_mwh=soc, verbose=False)
            if result['status'] != 'Optimal' or result['total_revenue_eur'] is None:
                raise RuntimeError(f'{strategy} {dt}: solver status {result["status"]}')
            results[strategy][dt] = {'opt_result': result, 'inputs': inputs}
            soc = result['soc'][-1]
            print(f'{strategy} | {dt} | revenue={result["total_revenue_eur"]:.2f} EUR | SOC={soc:.3f} MWh')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    print_result({'file_path': str(args.output_dir.resolve() / args.input.name), 'results': results}, config=config)


if __name__ == '__main__':
    main()
