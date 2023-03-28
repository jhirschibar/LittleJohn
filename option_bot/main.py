import argparse
import asyncio
import tracemalloc
from datetime import datetime

# import sentry_sdk
from dateutil.relativedelta import relativedelta

from option_bot.exceptions import InvalidArgs
from option_bot.orchestrator import (
    add_tickers_to_universe,
    import_all_tickers,
    remove_tickers_from_universe,
)


tracemalloc.start()
# sentry_sdk.init(
#     dsn="https://e76d761b19864956a5a95476a7a41f6a@o4504712959557632.ingest.sentry.io/4504782774337536",
#     traces_sample_rate=1.0,
# )


DEFAULT_DAYS = 500
DEFAULT_MONTHS_HIST = 24
DEFAULT_START_DATE = datetime.now() - relativedelta(months=DEFAULT_MONTHS_HIST)


async def add_ticker(args):
    await add_tickers_to_universe(
        [
            {
                "ticker": ticker,
                "start_date": args.startdate,
                "end_date": args.enddate,
                "months_hist": args.monthhist,
            }
            for ticker in args.tickers
        ]
    )


async def remove_tickers(args):
    tickers = list(args.tickers) if type(args.tickers) != list else args.tickers
    await remove_tickers_from_universe(tickers)


async def add_all_tickers(args):
    await import_all_tickers(args)


async def refresh_tickers(args):
    pass


async def refresh_all_tickers(args):
    pass


def main():
    """primary CLI for adding underlying stocks to our data universe, and specifying how far back to pull data"""

    parser = argparse.ArgumentParser(
        description="CLI for adding stocks to the data pull process and for refreshing stock/options pricing data"
    )

    parser.add_argument(
        "tickers",
        type=str,
        nargs="*",
        metavar="underlying tickers",
        help="Adds underlying tickers to our options data pull universe",
    )

    parser.add_argument(
        "-s",
        "--startdate",
        type=str,
        nargs=1,
        default=DEFAULT_START_DATE.strftime("%Y-%m"),
        metavar="YYYY-MM",
        help="YYYY-MM formatted date str indicating start of data pull for ticker stock price",
    )

    parser.add_argument(
        "-e",
        "--enddate",
        type=str,
        nargs=1,
        default=datetime.now().strftime("%Y-%m"),
        metavar="YYYY-MM",
        help="YYYY-MM formatted date str indicating end date of for ticker stock price",
    )

    parser.add_argument(
        "-m",
        "--monthhist",
        type=str,
        nargs=1,
        default=DEFAULT_MONTHS_HIST,
        metavar="int: Months of historical data",
        help="The number of months of historical options contracts you are going to pull",
    )

    parser.add_argument(
        "-r",
        "--remove",
        action="store_true",
        help="Removes the specified underlying ticker(s) from our options data pull universe",
    )

    parser.add_argument(
        "-aa",
        "--add-all",
        default=False,
        action="store_true",
        help="Adds all stocks tickers to the command. This works with refresh, or adding tickers",
    )

    parser.add_argument(
        "-ref",
        "--refresh",
        default=False,
        action="store_true",
        help="Removes the specified underlying ticker(s) from our options data pull universe",
    )

    args = parser.parse_args()
    args.startdate = datetime.strptime(args.startdate, "%Y-%m")
    args.enddate = datetime.strptime(args.enddate, "%Y-%m")

    if args.remove:
        if args.add_all:
            raise InvalidArgs("Can't --remove and --add_all at the same time. Remove explicit tickers via CLI")
        asyncio.run(remove_tickers(args))

    elif args.refresh:
        asyncio.run(refresh_tickers(args))

    else:
        if args.add_all:
            asyncio.run(add_all_tickers(args))
        else:
            asyncio.run(add_ticker(args))


if __name__ == "__main__":
    main()
