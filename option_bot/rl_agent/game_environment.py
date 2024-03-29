import asyncio
from decimal import Decimal
from datetime import datetime
from math import log
import random
from dataclasses import dataclass

import pandas as pd
import numpy as np
from py_vollib_vectorized.api import price_dataframe
from sklearn.preprocessing import StandardScaler
from torch import long
from torch.nn.functional import normalize  # use this OR sklearn scaler

from rl_agent.queries import extract_game_market_data
from rl_agent.constants import DAYS_TIL_EXP, ANNUAL_TRADING_DAYS, RISK_FREE, ACTIONS, FEATURE_COLS
from rl_agent.exceptions import InvalidStep, InvalidReset, EmptyInit
from db_tools.schemas import ContractType
from option_bot.utils import trading_days_in_range
from rl_agent.utils import dataframe_to_dict
from am_pm.port_tools.calc_funcs import calc_log_returns, calc_pct_returns, calc_hist_volatility


@dataclass
class Position:
    orig_price: Decimal
    long_short: int
    status: str
    nom_return: float
    pct_return: float


class GameEnvironment(object):
    """The game environment for the reinforcement learning agent.
    This env will support a single underlying ticker and up to 4 options positions
    Supported actions: close, hold position (2)
    The action space will be the number of combinations of actions per position. 2^n where n is [1,4]
    """

    position_strats = {
        1: ["call", "put"],
        2: ["call_credit_spread", "put_credit_spread", "staddle", "strangle"],
        3: ["double_call_credit_spread", "double_put_credit_spread", "strap", "strip"],
        4: ["iron_condor", "butterfly_spread"],
    }
    long_short_labels = {0: "LONG", 1: "SHORT"}

    actions_labels = dict(zip(range(len(ACTIONS)), ACTIONS))
    position_status = ["open", "closed"]
    contract_types = {"call": 0, "put": 1}

    underlying_cols = FEATURE_COLS[:8]
    option_cols = FEATURE_COLS[8:-2]

    def __init__(
        self, underlying_ticker: str, start_date: str | datetime, days_to_exp: int = DAYS_TIL_EXP, num_positions=1
    ):
        self.ticker = underlying_ticker
        self.data_start_date = datetime.strptime(start_date, "%Y-%m-%d") if type(start_date) == str else start_date
        self.start_days_to_exp = days_to_exp
        self.days_to_exp = days_to_exp
        self.num_positions = num_positions
        self.opt_tkrs: list[str] = []
        self.game_start_date: datetime = None
        self.game_date_index: pd.Series = None
        self.game_current_date_ix: int = None
        self.game_rewards: dict = {}
        self.game_positions: dict = {}
        self.end: bool = False
        self.state_data_df: pd.DataFrame = pd.DataFrame()
        self.underlying_price_df: pd.DataFrame = pd.DataFrame()

    def __repr__(self):
        data_loaded = True if self.state_data_df.shape[0] > 0 else False
        game_started = True if len(self.game_positions) > 0 else False
        return f"GameEnvironment(ticker={self.ticker}, data_start_date={self.data_start_date.date()}, days_to_exp={self.days_to_exp}, num_of_positions={self.num_positions}, data_loaded={data_loaded}, game_started={game_started}, game_ended={self.end})"

    # NOTE: should this only pull for the current game and be re-called at "reset"?
    async def _pull_game_price_data(self):
        # return await asyncio.gather(
        #     extract_ticker_price(self.ticker),
        #     extract_options_contracts(self.ticker, self.start_date),
        #     extract_options_prices(self.ticker, self.start_date),
        # )
        return await extract_game_market_data(self.ticker, self.data_start_date)

    # NOTE: may want to pull data from before the start date to calc hist volatility, etc

    async def prepare_state_data(self):
        # s_price, o_contracts, o_prices = await self.pull_game_price_data()
        df = await self._pull_game_price_data()
        df["flag"] = np.where(df["contract_type"] == ContractType.call, "c", "p")
        df["flag_put"] = np.where(df["contract_type"] == ContractType.put, 1, 0)

        # calc the time to expiration
        df["DTE"] = np.vectorize(trading_days_in_range)(df["as_of_date"], df["expiration_date"], "o_cal")
        # df['DTE'] = df.apply(
        #     lambda x: trading_days_in_range(x["as_of_date"], x["expiration_date"], "o_cal"),
        #     axis=1,
        # )

        # NOTE: THIS IS SLOW! Need to optimize with cuDF or np.vectorize
        # reference: https://shubhanshugupta.com/speed-up-apply-function-pandas-dataframe/#3-rapids-cudf-

        df["T"] = df["DTE"] / ANNUAL_TRADING_DAYS

        # add the risk free rate
        df = df.merge(RISK_FREE, on="as_of_date").sort_values("as_of_date").reset_index(drop=True)

        # calc the div yield
        # NOTE: div yield is not currently in the db

        # calc the implied volatility and greeks
        price_dataframe(
            df,
            flag_col="flag",
            underlying_price_col="stock_close_price",
            strike_col="strike_price",
            annualized_tte_col="T",
            riskfree_rate_col="risk_free_rate",
            price_col="opt_close_price",
            model="black_scholes",  # _merton when you add dividend yield
            inplace=True,
        )

        # split out the underlying and the state dfs

        self.state_data_df = df
        self.underlying_price_df = (
            df[["as_of_date"] + self.underlying_cols[0:3]]
            .drop_duplicates()
            .sort_values("as_of_date")
            .reset_index(drop=True)
        )

        # calc the log returns, pct returns, and historical volatility on underlying

        self.underlying_price_df["log_returns"] = calc_log_returns(
            self.underlying_price_df["stock_close_price"].to_numpy(dtype="float64")
        )
        self.underlying_price_df["pct_returns"] = calc_pct_returns(
            self.underlying_price_df["stock_close_price"].to_numpy(dtype="float64")
        )
        self.underlying_price_df["hist_90_vol"] = calc_hist_volatility(
            self.underlying_price_df["log_returns"].to_numpy(dtype="float64"), 90
        )
        self.underlying_price_df["hist_30_vol"] = calc_hist_volatility(
            self.underlying_price_df["log_returns"].to_numpy(dtype="float64"), 30
        )

        self.state_data_df = (
            self.state_data_df.merge(
                self.underlying_price_df[["as_of_date"] + self.underlying_cols[3:7]], on="as_of_date"
            )
            .sort_values("as_of_date")
            .reset_index(drop=True)
        )

    def _impute_missing_data(self):
        # use this https://github.com/rsheftel/pandas_market_calendars to find missing days
        pass

    def _normalize_state_data(self):
        """normalize transactions and volume and other figures so that the gradients don't explode"""
        scaler = StandardScaler()

    def _state_to_tensor(self, df: pd.DataFrame):
        """convert the state data to a tensor"""

    def reset(self):
        """self.days_to_exp is the counter within the game
        self.start_days_to_exp is the original value that self.days_to_exp is reset to. Set on class init()
        self.positions is a dict with a list [initial value of option position, long_short_position] for each option contract

        Returns:
            state: (pd.DataFrame) First state of the game. One row of the df for each option contract
            positions: (dict[str, Position]) the initial positions of the game. {opt_tkr: Position}

        """
        init = False
        if self.state_data_df.shape[0] == 0:
            raise InvalidReset("The state data has not been loaded. Please call prepare_state_data() first.")

        self.days_to_exp = self.start_days_to_exp
        init_count = 0
        while not init:
            try:
                (
                    self.game_start_date,
                    self.under_start_price,
                    self.opt_tkrs,
                    self.game_date_index,
                    long_short_positions,
                ) = self._init_random_positions()
                init = True
                # print(f"init count: {init_count}")
            except EmptyInit:
                init_count += 1
        self.game_state = (
            self.state_data_df.loc[
                (self.state_data_df["as_of_date"] >= self.game_start_date)
                & (self.state_data_df["options_ticker"].isin(self.opt_tkrs))
            ]
            .sort_values("as_of_date", ascending=True)
            .reset_index(drop=True)
        )
        self.game_positions = {
            self.opt_tkrs[i]: Position(  # dataclass
                self.game_state.loc[self.game_state["options_ticker"] == self.opt_tkrs[i]].iloc[0][
                    "opt_close_price"
                ],  # original price
                long_short_positions[i],  # long or short
                "open",  # status
                0.0,  # nominal return
                0.0,  # percent return
            )
            for i in range(len(self.opt_tkrs))
        }
        self.game_rewards = {opt_tkr: [] for opt_tkr in self.opt_tkrs}
        self.end = False
        self.game_current_date_ix = self.underlying_price_df.loc[
            self.underlying_price_df["as_of_date"] == self.game_start_date
        ].index[0]
        return self.game_state.loc[self.game_state["as_of_date"] == self.game_start_date], self.game_positions

    def step(self, actions: dict[str, int], current_state: pd.DataFrame):
        """Function thatreturns the next state, reward, and whether the game is over based on the input actions.
        It extracts the next state from the game_state_df based on the next as_of_date.
        It then converts state to dicts with keys being the opt_tickers
        If there were no transactions for a given ticker, it will use the previous state's option values for that ticker.
        It then calculates the rewards for the actions and sums into an aggregate reward

        Args:
            actions: list[int]
                the actions to take for each position. len(actions) == self.num_positions and in the same order
            current_state: pd.DataFrame
                the current state of the game.

        Returns:
            next_state: pd.DataFrame
                the next state of the next step of the game. The next row in game_state_df with index of the next as_of_date
            game_positions: dict[str, Position]
                the current position of each option contract in the game
            game_reward: dict[str, list[float]]
                the pct_point reward for each position in the game
        """
        if self.end:
            raise InvalidStep("The game has ended. Please reset the game to continue.")
        # count down days to expiration
        self.days_to_exp -= 1
        if (
            self.days_to_exp == 0 or sum(actions.values()) == 0
        ):  # sum(actions) will = 0 when the last position is being closed
            self.end = True
            return current_state, self.game_positions, self.game_rewards

        # retrieve data for the underlying stock for the next day
        self.game_current_date_ix += 1
        new_date = self.underlying_price_df["as_of_date"].iloc[self.game_current_date_ix]
        underlying_state = self.underlying_price_df.loc[self.underlying_price_df["as_of_date"] == new_date].to_dict(
            "records"
        )[0]

        # calculate the new state, backfilling with options data from the previous state if no transactions on the new_date
        next_state = self.game_state.loc[self.game_state["as_of_date"] == new_date]
        current_state = dataframe_to_dict(df=current_state, index_key="options_ticker")
        next_state = dataframe_to_dict(df=next_state, index_key="options_ticker")
        for tkr in self.opt_tkrs:
            if tkr not in next_state:
                next_state[tkr] = current_state[tkr]
                for k, v in underlying_state.items():
                    next_state[tkr][k] = v
                next_state[tkr]["as_of_date"] = new_date  # should be redundant after underlying_state update
                next_state[tkr]["opt_volume"] = 0
                next_state[tkr]["opt_number_of_transactions"] = 0
                next_state[tkr]["DTE"] -= 1
                next_state[tkr]["T"] = next_state[tkr]["DTE"] / ANNUAL_TRADING_DAYS

        # calculate the reward
        self._calc_reward(actions, current_state, next_state)
        next_state_df = pd.DataFrame([{**next_state[tkr], **{"options_ticker": tkr}} for tkr in next_state])
        return next_state_df, self.game_positions, self.game_rewards

    def _calc_reward(self, actions: dict[str, int], current_state: pd.DataFrame, next_state: pd.DataFrame) -> float:
        """
        calculates the reward for the current state (the percentage point change for the day)
        as well as calcs the nominal and percent return for each option contract in the game_positions

        NOTE: potentially will isolate reward per option in future
        Also, may calculate reward as percent return on collateral per day or some ratio like that.
        If there is a third possible action, this if/else will need to be changed
        """
        for tkr in self.opt_tkrs:
            if self.game_positions[tkr].status == "open":
                if self.actions_labels[actions[tkr]] == "CLOSE POSITION":
                    self.game_rewards[tkr].append(0)
                    self.game_positions[tkr].status = "closed"
                    self.game_positions[tkr].nom_return = 0
                    self.game_positions[tkr].pct_return = 0
                else:
                    new_price = float(next_state[tkr]["opt_close_price"])
                    old_price = float(current_state[tkr]["opt_close_price"])
                    nom_reward = new_price - old_price

                    if self.long_short_labels[self.game_positions[tkr].long_short] == "SHORT":
                        self.game_rewards[tkr].append(-1 * nom_reward / float(self.game_positions[tkr].orig_price))

                        self.game_positions[tkr].nom_return += -1 * nom_reward
                        self.game_positions[tkr].pct_return = 1 - (
                            new_price / float(self.game_positions[tkr].orig_price)
                        )
                    else:
                        self.game_rewards[tkr].append(nom_reward / float(self.game_positions[tkr].orig_price))

                        self.game_positions[tkr].nom_return += nom_reward
                        self.game_positions[tkr].pct_return = new_price / float(self.game_positions[tkr].orig_price)
            else:
                self.game_rewards[tkr].append(0)

    def _init_random_positions(self) -> list[str]:
        """this function initializes the game with random positions.
        It chooses a row from the self.underlying_price_df that is atleast self.days_to_exp positions away from the last row.
        It takes the as_of_date value and the stock_close_price from that row.
        It then filters the self.state_data_df to only include rows with that as_of_date and chooses self.num_positions options contracts whose strike prices are +/- 8 contracts away from the stock_close_price.

        Returns:
            start_date: datetime
                the date that is the basis for current observations.
            under_start_price: decimal
                The current price of the underlying ticker
            opt_tkrs: List[str]
                the options contract tickers, len = self.num_positions
            game_date_index: pd.Series
                the index of the dates that will be used for the game. len = self.days_to_exp + 1
            long_short_positions: List[int]
                the long or short positions for each option contract. len = self.num_positions

        NOTE: may use under_start_price to decide if options should only be in the money or out of the money. But that can be done later.
        Or, to make sure that the strike price is within some range of the underlying price.
        Otherwise we don't need to return it as long as we have the start_date

        Also: starting with only short positions for now

        TODO: remove the magic numbers
        """
        ix = random.randint(0, len(self.underlying_price_df) - self.days_to_exp)
        start_date, under_start_price = self.underlying_price_df.iloc[ix][["as_of_date", "stock_close_price"]].values
        opt_tkrs_df = (
            self.state_data_df.loc[
                (self.state_data_df["as_of_date"] == start_date)
                & (self.state_data_df["DTE"] >= self.days_to_exp)
                & (self.state_data_df["DTE"] <= self.days_to_exp + 15)  # NOTE: this is a magic number
                & (~self.state_data_df["IV"].isna())
            ]
            .head(50)  # NOTE: this is a magic number
            .sort_values(by=["expiration_date", "opt_number_of_transactions"], ascending=[True, False])
            .reset_index(drop=True)
        )
        if opt_tkrs_df.empty:
            print(f"No qualifying options for {start_date}, ix: {ix}")
            raise EmptyInit(f"No qualifying options for this random position")
        opt_tkr_ix = []
        while len(opt_tkr_ix) < self.num_positions:
            opt_ix = random.randint(0, opt_tkrs_df.shape[0] - 1)
            if opt_ix not in opt_tkr_ix:
                opt_tkr_ix.append(opt_ix)

        opt_tkrs = opt_tkrs_df.iloc[opt_tkr_ix]["options_ticker"].tolist()

        game_date_index = self.underlying_price_df["as_of_date"].iloc[ix : ix + self.days_to_exp + 1]
        long_short_positions = [
            1 for i in range(self.num_positions)  # sets all as short for now
        ]  # [random.randint(0, 1) for i in range(self.num_positions)]
        return start_date, under_start_price, opt_tkrs, game_date_index, long_short_positions


def calc_port_return_from_positions(positions: dict[str, Position]) -> float:
    """Calculate the pct return from the positions in the portfolio"""
    return sum([positions[tkr].pct_return for tkr in positions.keys()])


if __name__ == "__main__":
    pass
