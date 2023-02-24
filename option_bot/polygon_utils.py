import asyncio
import math
import time
from datetime import datetime
from decimal import Decimal
from enum import Enum

import numpy as np
from aiohttp import request
from aiomultiprocess import Pool
from proj_constants import log, POLYGON_API_KEY
from utils import first_weekday_of_month, timestamp_to_datetime


class Timespans(Enum):
    minute = "minute"
    hour = "hour"
    day = "day"
    week = "week"
    month = "month"
    quarter = "quarter"
    year = "year"


class PolygonPaginator(object):
    """API paginator interface for calls to the Polygon API. \
        It tracks queries made to the polygon API and calcs potential need for sleep"""

    MAX_QUERY_PER_MINUTE = 4  # free api limits to 5 / min which is 4 when indexed at 0
    polygon_api = "https://api.polygon.io"

    def __init__(self):  # , query_count: int = 0):
        self.query_count = 0  # = query_count
        self.query_time_log = []
        self.results = []
        self.clean_results = []
        self.clean_data_generator = iter(())

    def _api_sleep_time(self) -> int:
        sleep_time = 60
        if len(self.query_time_log) > 2:
            a = timestamp_to_datetime(self.query_time_log[0]["query_timestamp"])
            b = timestamp_to_datetime(self.query_time_log[-1]["query_timestamp"])
            diff = math.ceil((b - a).total_seconds())
            sleep_time = diff if diff < sleep_time else sleep_time
        return sleep_time

    async def query_all(self, url: str, payload: dict = {}, overload=False):
        payload["apiKey"] = POLYGON_API_KEY
        if (self.query_count >= self.MAX_QUERY_PER_MINUTE) or overload:
            await asyncio.sleep(self._api_sleep_time())
            self.query_count = 0
            self.query_time_log = []

        log.info(f"{url} {payload} overload:{overload}")
        async with request(method="GET", url=url, params=payload) as response:
            log.info(f"status code: {response.status}")

            self.query_count += 1

            if response.status == 200:
                results = await response.json()
                self.query_time_log.append({"request_id": results.get("request_id"), "query_timestamp": time.time()})
                self.results.append(results)
                next_url = results.get("next_url")
                if next_url:
                    await self.query_all(next_url)
            elif response.status == 429:
                await self.query_all(url, payload, overload=True)
            else:
                response.raise_for_status()

    def make_clean_generator(self):
        record_size = len(self.clean_results[0])
        batch_size = round(60000 / record_size)  # postgres input limit is ~65000
        for i in range(0, len(self.clean_results), batch_size):
            yield self.clean_results[i : i + batch_size]

    async def query_data(self):
        """shell function to be overwritten by every inheriting class"""
        log.exception("Function undefined in inherited class")
        raise Exception

    def clean_data(self):
        """shell function to be overwritten by every inheriting class"""
        log.exception("Function undefined in inherited class")
        raise Exception

    async def fetch(self):
        await self.query_data()
        self.clean_data()
        self.clean_data_generator = self.make_clean_generator()


class StockMetaData(PolygonPaginator):
    """Object to query the Polygon API and retrieve information about listed stocks. \
        It can be used to query for a single individual ticker or to pull the entire corpus"""

    def __init__(self, ticker: str, all_: bool):
        self.ticker = ticker
        self.all_ = all_
        self.payload = {"active": True, "market": "stocks", "limit": 1000}
        super().__init__()

    async def query_data(self):
        """"""
        url = self.polygon_api + "/v3/reference/tickers"
        if not self.all_:
            self.payload["ticker"] = self.ticker
        await self.query_all(url=url, payload=self.payload)

    def clean_data(self):
        selected_keys = [
            "ticker",
            "name",
            "type",
            "active",
            "market",
            "locale",
            "primary_exchange",
            "currency_name",
            "cik",
        ]
        for result_list in self.results:
            for ticker in result_list["results"]:
                t = {x: ticker.get(x) for x in selected_keys}
                self.clean_results.append(t)


class HistoricalStockPrices(PolygonPaginator):
    """Object to query Polygon API and retrieve historical prices for the underlying stock"""

    def __init__(
        self,
        ticker: str,
        ticker_id: int,
        start_date: datetime,
        end_date: datetime,
        multiplier: int = 1,
        timespan: Timespans = Timespans.day,
        adjusted: bool = True,
    ):
        self.ticker = ticker
        self.ticker_id = ticker_id
        self.multiplier = multiplier
        self.timespan = timespan.value
        self.start_date = start_date.date()
        self.end_date = end_date.date()
        self.adjusted = adjusted
        super().__init__()

    async def query_data(self):
        url = (
            self.polygon_api
            + f"/v2/aggs/ticker/{self.ticker}/range/{self.multiplier}/{self.timespan}/{self.start_date}/{self.end_date}"
        )
        payload = {"adjusted": self.adjusted, "sort": "desc", "limit": 50000}
        await self.query_all(url, payload)

    def clean_data(self):
        key_mapping = {
            "v": "volume",
            "vw": "volume_weight_price",
            "c": "close_price",
            "o": "open_price",
            "h": "high_price",
            "l": "low_price",
            "t": "as_of_date",
            "n": "number_of_transactions",
            "otc": "otc",
        }
        for page in self.results:
            for record in page.get("results"):
                t = {key_mapping[key]: record.get(key) for key in key_mapping}
                t["as_of_date"] = timestamp_to_datetime(t["as_of_date"], msec_units=True)
                t["ticker_id"] = self.ticker_id
                self.clean_results.append(t)


class OptionsContracts(PolygonPaginator):
    """Object to query options contract tickers for a given underlying ticker based on given dates.

    Attributes:
        ticker: str
            the underlying stock ticker
        base_date: [datetime]
            the date that is the basis for current observations. \
            In other words: the date at which you are looking at the chain of options data
        current_price: decimal
            The current price of the underlying ticker
    """

    def __init__(self, ticker: str, ticker_id: int, months_hist: int = 24, cpu_count: int = 1, all_: bool = False):
        self.ticker = ticker
        self.ticker_id = ticker_id
        self.months_hist = months_hist
        self.cpu_count = cpu_count
        self.base_dates = self._determine_base_dates()
        self.all_ = all_
        super().__init__()

    def _determine_base_dates(self) -> list[datetime]:
        year_month_array = []
        counter = 0
        year = datetime.now().year
        month = datetime.now().month
        while counter <= self.months_hist:
            year_month_array.append(
                str(year) + "-" + str(month) if len(str(month)) > 1 else str(year) + "-0" + str(month)
            )
            if month == 1:
                month = 12
                year -= 1
            else:
                month -= 1
            counter += 1
        return [str(x) for x in first_weekday_of_month(np.array(year_month_array)).tolist()]

    async def query_data(self):
        url = self.polygon_api + "/v3/reference/options/contracts"
        payload = {"limit": 1000}
        if not self.all_:
            payload["underlying_ticker"] = self.ticker
        args = [[url, dict(payload, **{"as_of": date})] for date in self.base_dates]
        async with Pool(processes=self.cpu_count) as pool:
            async for result in pool.starmap(self.query_all, args):
                self.results.append(result)

    def clean_data(self):
        key_mapping = {
            "ticker": "option_ticker",
            "expiration_date": "expiration_date",
            "strike_price": "strike_price",
            "contract_type": "contract_type",
            "shares_per_contract": "shares_per_contract",
            "primary_exchange": "primary_exchange",
            "exercise_style": "exercise_style",
            "cfi": "cfi",
        }
        for page in self.results:
            for record in page.get("results"):
                t = {key_mapping[key]: record.get(key) for key in key_mapping}
                t["underlying_ticker_id"] = self.ticker_id
                self.clean_results.append(t)
                self.clean_results = list({v["option_ticker"]: v for v in self.clean_results}.values())


class HistoricalOptionsPrices(PolygonPaginator):
    """Object to query Polygon API and retrieve historical prices for the options chain for a given ticker

    Attributes:
        options_tickers: List[str]
            the options contract tickers

        exp_date: [datetime, datetime]
            the range of option expiration dates to be queried

        strike_price: decimal
            the strike price range want to include in our queries

    Note:
        exp_date and strike_price are inclusive ranges
    """

    def __init__(
        self,
        tickers: list[str],
        base_date: datetime,
    ):
        self.o_tickers = tickers
        self.base_date = base_date  # as_of date
        self.ticker_list = self._options_tickers_constructor()

    def _window_of_focus_dates(self):
        """"""
        return

    def _time_conversion(self):
        return

    def _clean_api_results(self, ticker: str) -> list[dict]:
        clean_results = []
        return clean_results

    def get_historical_prices(
        self, start_date: datetime, end_date: datetime, timespan: str = "day", multiplier: int = 1
    ):
        """api call to the aggs endpoint

        Parameters:
            start_date (datetime): beginning of date range for historical query (date inclusive)
            end_date (datetime): ending of date range for historical query (date inclusive)
            timespan (str) : the default value is set to "day". \
                Options are ["minute", "hour", "day", "week", "month", "quarter", "year"]
            multiplier (int) : multiples of the timespan that should be included in the call. Defaults to 1

        """
        # TODO: implement async/await so it pulls and processes more quickly
        # TODO: pull the function inputs from self, not as inputs
        self.hist_prices = []
        for ticker in self.ticker_list:
            url = self.polygon_api + f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{start_date}/{end_date}"
            self.query_all(url)
            ticker_results = self._clean_api_results(ticker)
            self.hist_prices.append(ticker_results)

    def query_data(self):
        pass

    def clean_data(self):
        pass


# TODO: figure out how you are going to handle data refreshing. Simply update the whole history?
# Or append and find a way to adjust for splits?

# TODO: Make all functions with these classes async ready, and make the classes ready to manage multiple workers