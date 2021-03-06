#!/usr/bin/env python

import asyncio
import importlib.util
import sys
import traceback
import uuid
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import alpaca_trade_api as tradeapi
import nest_asyncio
import pandas as pd
import pytz
from requests.exceptions import HTTPError
from tabulate import tabulate

from liualgotrader.analytics.analysis import load_trades_by_batch_id
from liualgotrader.common import config, market_data, trading_data
from liualgotrader.common.database import create_db_connection
from liualgotrader.common.decorators import timeit
from liualgotrader.common.tlog import tlog
from liualgotrader.fincalcs.vwap import add_daily_vwap
from liualgotrader.models.algo_run import AlgoRun
from liualgotrader.models.new_trades import NewTrade
from liualgotrader.models.trending_tickers import TrendingTickers
from liualgotrader.scanners.base import Scanner
from liualgotrader.scanners.momentum import Momentum
from liualgotrader.strategies.base import Strategy, StrategyType


def get_batch_list():
    @timeit
    async def get_batch_list_worker():
        await create_db_connection()
        data = await AlgoRun.get_batches()
        print(
            tabulate(
                data,
                headers=["build", "batch_id", "strategy", "env", "start time"],
            )
        )

    try:
        if not asyncio.get_event_loop().is_closed():
            asyncio.get_event_loop().close()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(asyncio.new_event_loop())
        loop.run_until_complete(get_batch_list_worker())
    except KeyboardInterrupt:
        tlog("get_batch_list() - Caught KeyboardInterrupt")
    except Exception as e:
        tlog(
            f"get_batch_list() - exception of type {type(e).__name__} with args {e.args}"
        )
        traceback.print_exc()


"""
starting
"""


def show_usage():
    print(
        f"usage: {sys.argv[0]} --batch-list OR [--strict] [--symbol=SYMBOL] [--debug=SYMBOL] [--duration=<minutes>] <batch-id>\n"
    )
    msg = """
    'backter' application re-runs a past trading session, with new or modified
     strategies specified in tradeplan.toml. 'backter' application looks for 
     tradeplan.toml in current directory. The 'backter' application expects a 
     batch-id (UDID) as input. Using the --batch-list option you 
     can see a list of recent sessions to choose from. 
    """
    print(msg)
    print("options:")
    print(
        "--batch-list\tDisplay list of trading sessions, list limited to last 30 days"
    )
    print("--symbol\tRun on specific SYMBOL, bypass batch-id scanners")
    print(
        "--duration\tRun back-test for number of <minutes>, bypass batch-id run duration"
    )
    print(
        "--debug\tWrite verbose debug information for symbol SYMBOL during back-testing"
    )
    print(
        "--strict\tRun back-test session only on same symbols traded in the original batch"
    )


def show_version(filename: str, version: str) -> None:
    """Display welcome message"""
    print(f"filename:{filename}\ngit version:{version}\n")


async def create_strategies(
    conf_dict: Dict,
    duration: timedelta,
    ref_run_id: Optional[int],
    uid: str,
    start: datetime,
    bypass_strategy_duration: bool = False,
) -> None:
    strategy_types = []
    for strategy in conf_dict["strategies"]:
        print(strategy)
        strategy_name = strategy
        strategy_details = conf_dict["strategies"][strategy_name]
        if strategy_name == "MomentumLong":
            tlog(f"strategy {strategy_name} selected")
            strategy_types += [(strategy_details)]
        else:
            tlog(f"custom strategy {strategy_name} selected")

            try:
                spec = importlib.util.spec_from_file_location(
                    "module.name", strategy_details["filename"]
                )
                custom_strategy_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(custom_strategy_module)  # type: ignore
                class_name = strategy_name
                custom_strategy = getattr(custom_strategy_module, class_name)

                if not issubclass(custom_strategy, Strategy):
                    tlog(
                        f"custom strartegy must inherit from class {Strategy.__name__}"
                    )
                    exit(0)
                strategy_details.pop("filename", None)
                strategy_types += [(custom_strategy, strategy_details)]

            except Exception as e:
                tlog(
                    f"[Error]exception of type {type(e).__name__} with args {e.args}"
                )
                traceback.print_exc()
                exit(0)

    for strategy_tuple in strategy_types:
        strategy_type = strategy_tuple[0]
        strategy_details = strategy_tuple[1]
        config.env = "BACKTEST"
        tlog(f"initializing {strategy_type.name}")

        if "schedule" not in strategy_details:
            print("duration:", duration)
            strategy_details["schedule"] = [
                {
                    "start": int(
                        (
                            start - start.replace(hour=13, minute=30)
                        ).total_seconds()
                        // 60
                    ),
                    "duration": int(duration.total_seconds() // 60),
                }
            ]
        if bypass_strategy_duration:
            for schedule in strategy_details["schedule"]:
                schedule["duration"] = duration.total_seconds() // 60

        s = strategy_type(
            batch_id=uid, ref_run_id=ref_run_id, **strategy_details
        )
        await s.create()
        trading_data.strategies.append(s)


@timeit
async def backtest_symbol(
    data_api: tradeapi,
    portfolio_value: float,
    symbol: str,
    start: datetime,
    duration: timedelta,
    scanner_start_time: datetime,
    debug_symbol: bool = False,
) -> None:
    est = pytz.timezone("America/New_York")
    scanner_start_time = (
        pytz.utc.localize(scanner_start_time).astimezone(est)
        if scanner_start_time.tzinfo is None
        else scanner_start_time
    )
    start_time = pytz.utc.localize(start).astimezone(est)

    if scanner_start_time > start_time + duration:
        print(
            f"{symbol} picked too late at {scanner_start_time} ({start_time}, {duration})"
        )
        return

    start_time = scanner_start_time
    if start_time.second > 0:
        start_time = start_time.replace(second=0, microsecond=0)
    print(
        f"--> back-testing {symbol} from {str(start_time)} duration {duration}"
    )
    if debug_symbol:
        print("--> using DEBUG mode")

    re_try = 3

    while re_try > 0:
        # load historical data
        try:
            symbol_data = data_api.polygon.historic_agg_v2(
                symbol,
                1,
                "minute",
                _from=str(start_time - timedelta(days=8)),
                to=str(start_time + timedelta(days=1)),
                limit=10000,
            ).df
        except HTTPError as e:
            tlog(f"Received HTTP error {e} for {symbol}")
            return

        if len(symbol_data) < 100:
            tlog(f"not enough data-points  for {symbol}")
            return

        add_daily_vwap(
            symbol_data,
            debug=debug_symbol,
        )
        market_data.minute_history[symbol] = symbol_data
        print(
            f"loaded {len(market_data.minute_history[symbol].index)} agg data points"
        )
        try:
            minute_index = symbol_data["close"].index.get_loc(
                start_time, method="nearest"
            )
            break
        except (Exception, ValueError) as e:
            print(f"[EXCEPTION] {e} - trying to reload-data. ")
            re_try -= 1

    if re_try <= 0:
        return

    position: int = 0
    new_now = symbol_data.index[minute_index]
    print(f"start time with data {new_now}")
    price = 0.0
    last_run_id = None
    # start_time + duration
    while (
        new_now < config.market_close
        and minute_index < symbol_data.index.size - 1
    ):
        if symbol_data.index[minute_index] != new_now:
            print("mismatch!", symbol_data.index[minute_index], new_now)
            print(symbol_data["close"][minute_index - 10 : minute_index + 1])
            raise Exception()

        price = symbol_data["close"][minute_index]
        for strategy in trading_data.strategies:
            if debug_symbol:
                print(
                    f"Execute strategy {strategy.name} on {symbol} at {new_now}"
                )

            try:
                do, what = await strategy.run(
                    symbol,
                    True,
                    position,
                    symbol_data[: minute_index + 1],
                    new_now,
                    portfolio_value,
                    debug=debug_symbol,  # type: ignore
                    backtesting=True,
                )
            except Exception as e:
                tlog(
                    f"[ERROR] exception {e} on symbol {symbol} @ {strategy.name}"
                )
                continue

            if do:
                if (
                    what["side"] == "buy"
                    and float(what["qty"]) > 0
                    or what["side"] == "sell"
                    and float(what["qty"]) < 0
                ):
                    position += int(float(what["qty"]))
                    trading_data.buy_time[symbol] = new_now.replace(
                        second=0, microsecond=0
                    )
                else:
                    position -= int(float(what["qty"]))

                trading_data.last_used_strategy[symbol] = strategy

                db_trade = NewTrade(
                    algo_run_id=strategy.algo_run.run_id,
                    symbol=symbol,
                    qty=int(float(what["qty"])),
                    operation=what["side"],
                    price=price,
                    indicators=trading_data.buy_indicators[symbol]
                    if what["side"] == "buy"
                    else trading_data.sell_indicators[symbol],
                )

                await db_trade.save(
                    config.db_conn_pool,
                    str(new_now),
                    trading_data.stop_prices[symbol],
                    trading_data.target_prices[symbol],
                )

                if what["side"] == "buy":
                    await strategy.buy_callback(
                        symbol, price, int(float(what["qty"]))
                    )
                    break
                elif what["side"] == "sell":
                    await strategy.sell_callback(
                        symbol, price, int(float(what["qty"]))
                    )
                    break
            last_run_id = strategy.algo_run.run_id

        minute_index += 1
        new_now = symbol_data.index[minute_index]

    if position:
        if (
            trading_data.last_used_strategy[symbol].type
            == StrategyType.DAY_TRADE
        ):
            tlog(f"[{new_now}]{symbol} liquidate {position} at {price}")
            db_trade = NewTrade(
                algo_run_id=last_run_id,  # type: ignore
                symbol=symbol,
                qty=int(position) if int(position) > 0 else -int(position),
                operation="sell" if position > 0 else "buy",
                price=price,
                indicators={"liquidate": 1},
            )
            await db_trade.save(
                config.db_conn_pool,
                str(symbol_data.index[minute_index - 1]),
            )


def backtest(
    batch_id: str,
    conf_dict: Dict,
    debug_symbols: List[str],
    strict: bool = False,
    specific_symbols: List[str] = None,
    bypass_duration: int = None,
) -> str:
    data_api: tradeapi = tradeapi.REST(
        base_url=config.prod_base_url,
        key_id=config.prod_api_key_id,
        secret_key=config.prod_api_secret,
    )
    portfolio_value: float = (
        100000.0 if not config.portfolio_value else config.portfolio_value
    )
    uid = str(uuid.uuid4())

    async def backtest_run(
        start: datetime,
        duration: timedelta,
        ref_run_id: int,
        specific_symbols: List[str] = None,
    ) -> None:
        if specific_symbols:
            symbols_and_start_time: List = []
            for symbol in specific_symbols:
                symbols_and_start_time.append((symbol, start))
            num_symbols = len(specific_symbols)
        elif not strict:
            symbols_and_start_time = await TrendingTickers.load(batch_id)

            num_symbols = len(symbols_and_start_time)
        else:
            print("strict mode selected, loading symbols from trades")
            nest_asyncio.apply()
            _df = load_trades_by_batch_id(batch_id)
            symbols = _df.symbol.unique().tolist()
            num_symbols = len(symbols)
            est = pytz.timezone("America/New_York")
            start_time = pytz.utc.localize(_df.start_time.min()).astimezone(
                est
            )
            symbols_and_start_time = list(
                zip(symbols, [start_time for x in range(num_symbols)])
            )

        print(f"loaded {len(symbols_and_start_time)} symbols")

        if num_symbols > 0:
            est = pytz.timezone("America/New_York")
            start_time = pytz.utc.localize(start).astimezone(est)
            config.market_open = start_time.replace(
                hour=9, minute=30, second=0, microsecond=0
            )
            config.market_close = start_time.replace(
                hour=16, minute=0, second=0, microsecond=0
            )
            print(f"market_open {config.market_open}")
            await create_strategies(
                conf_dict,
                duration,
                ref_run_id,
                uid,
                start,
                bypass_duration is not None,
            )

            for symbol_and_start_time in symbols_and_start_time:
                symbol = symbol_and_start_time[0]
                await backtest_symbol(
                    data_api=data_api,
                    portfolio_value=portfolio_value,
                    symbol=symbol,
                    start=start,
                    duration=duration,
                    scanner_start_time=symbol_and_start_time[1],
                    debug_symbol=True if symbol in debug_symbols else False,
                )

    @timeit
    async def backtest_worker() -> None:
        await create_db_connection()
        run_details = await AlgoRun.get_batch_details(batch_id)
        run_ids, starts, ends, _ = zip(*run_details)

        if not len(run_details):
            print(f"can't load data for batch id {batch_id}")
        else:
            await backtest_run(
                start=min(starts),
                duration=timedelta(
                    minutes=max(
                        [
                            w["duration"]
                            for w in [
                                item
                                for sublist in [
                                    conf_dict["strategies"][s]["schedule"]  # type: ignore
                                    for s in conf_dict["strategies"]  # type: ignore
                                ]
                                for item in sublist
                            ]
                        ]
                    )
                    if not bypass_duration
                    else bypass_duration
                ),
                ref_run_id=run_ids[0],
                specific_symbols=specific_symbols,
            )

    try:
        if not asyncio.get_event_loop().is_closed():
            asyncio.get_event_loop().close()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(asyncio.new_event_loop())
        loop.run_until_complete(backtest_worker())
    except KeyboardInterrupt:
        tlog("backtest() - Caught KeyboardInterrupt")
    except Exception as e:
        tlog(
            f"backtest() - exception of type {type(e).__name__} with args {e.args}"
        )
        traceback.print_exc()
    finally:
        print("=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=")
        print(f"new batch-id: {uid}")
        return uid


class BackTestDay:
    def __init__(self, conf_dict: Dict):
        self.uid = str(uuid.uuid4())

        self.data_api: tradeapi = tradeapi.REST(
            base_url=config.prod_base_url,
            key_id=config.prod_api_key_id,
            secret_key=config.prod_api_secret,
        )

        self.conf_dict = conf_dict
        config.portfolio_value = self.conf_dict.get("portfolio_value", None)
        self.minute_history: Dict[str, pd.DataFrame] = {}
        self.scanners: List[Scanner] = []

    async def create(self, day: date) -> str:
        await create_db_connection()
        scanners_conf = self.conf_dict["scanners"]

        est = pytz.timezone("America/New_York")
        start_time = datetime.combine(day, datetime.min.time()).astimezone(est)
        day = datetime.combine(day, datetime.min.time()).astimezone(est)
        self.start = day.replace(hour=9, minute=30)
        self.end = day.replace(hour=16, minute=0)

        config.market_open = start_time.replace(
            hour=9, minute=30, second=0, microsecond=0
        )
        config.market_close = start_time.replace(
            hour=16, minute=0, second=0, microsecond=0
        )
        for scanner_name in scanners_conf:
            scanner_object: Optional[Scanner] = None
            if scanner_name == "momentum":
                scanner_details = scanners_conf[scanner_name]
                try:
                    recurrence = scanner_details.get("recurrence", None)
                    target_strategy_name = scanner_details.get(
                        "target_strategy_name", None
                    )
                    scanner_object = Momentum(
                        provider=scanner_details["provider"],
                        data_api=self.data_api,
                        min_last_dv=scanner_details["min_last_dv"],
                        min_share_price=scanner_details["min_share_price"],
                        max_share_price=scanner_details["max_share_price"],
                        min_volume=scanner_details["min_volume"],
                        from_market_open=scanner_details["from_market_open"],
                        today_change_percent=scanner_details["min_gap"],
                        recurrence=timedelta(minutes=recurrence)
                        if recurrence
                        else None,
                        target_strategy_name=target_strategy_name,
                        max_symbols=scanner_details.get(
                            "max_symbols", config.total_tickers
                        ),
                    )
                    tlog(f"instantiated momentum scanner")
                except KeyError as e:
                    tlog(
                        f"Error {e} in processing of scanner configuration {scanner_details}"
                    )
                    exit(0)
            else:
                tlog(f"custom scanner {scanner_name} selected")
                scanner_details = scanners_conf[scanner_name]
                try:
                    spec = importlib.util.spec_from_file_location(
                        "module.name", scanner_details["filename"]
                    )
                    custom_scanner_module = importlib.util.module_from_spec(
                        spec
                    )
                    spec.loader.exec_module(custom_scanner_module)  # type: ignore
                    class_name = scanner_name
                    custom_scanner = getattr(custom_scanner_module, class_name)

                    if not issubclass(custom_scanner, Scanner):
                        tlog(
                            f"custom scanner must inherit from class {Scanner.__name__}"
                        )
                        exit(0)

                    scanner_details.pop("filename")
                    if "recurrence" not in scanner_details:
                        scanner_object = custom_scanner(
                            data_api=self.data_api,
                            **scanner_details,
                        )
                    else:
                        recurrence = scanner_details.pop("recurrence")
                        scanner_object = custom_scanner(
                            data_api=self.data_api,
                            recurrence=timedelta(minutes=recurrence),
                            **scanner_details,
                        )

                except Exception as e:
                    tlog(
                        f"[Error] scanners_runner.scanners_runner() for {scanner_name}:{e} "
                    )
            if scanner_object:
                self.scanners.append(scanner_object)

        await create_strategies(
            self.conf_dict,
            self.end - self.start,
            None,
            self.uid,
            day.replace(hour=9, minute=30, second=0, microsecond=0),
        )

        self.now = pd.Timestamp(self.start)
        self.symbols: List = []
        self.portfolio_value: float = (
            100000.0 if not config.portfolio_value else config.portfolio_value
        )
        if "risk" in self.conf_dict:
            config.risk = self.conf_dict["risk"]
        return self.uid

    async def next_minute(self) -> Tuple[bool, List[Optional[str]]]:
        rc_msg: List[Optional[str]] = []
        if self.now < self.end:
            for i in range(0, len(self.scanners)):
                if self.now == self.start or (
                    self.scanners[i].recurrence is not None
                    and self.scanners[i].recurrence.total_seconds() > 0  # type: ignore
                    and int((self.now - self.start).total_seconds() // 60)  # type: ignore
                    % int(self.scanners[i].recurrence.total_seconds() // 60)  # type: ignore
                    == 0
                ):
                    new_symbols = await self.scanners[i].run(self.now)
                    if new_symbols:
                        really_new = [
                            x for x in new_symbols if x not in self.symbols
                        ]
                        if len(really_new) > 0:
                            print(
                                f"Loading data for {len(really_new)} symbols: {really_new}"
                            )
                            rc_msg.append(
                                f"Loaded data for {len(really_new)} symbols: {really_new}"
                            )
                            self.minute_history = {
                                **self.minute_history,
                                **(
                                    market_data.get_historical_data_from_poylgon_for_symbols(
                                        self.data_api,
                                        really_new,
                                        self.start - timedelta(days=7),
                                        self.start + timedelta(days=1),
                                    )
                                ),
                            }
                            self.symbols += really_new
                            print(f"loaded data for {len(really_new)} stocks")

            for symbol in self.symbols:
                try:
                    for strategy in trading_data.strategies:

                        try:
                            minute_index = self.minute_history[symbol][
                                "close"
                            ].index.get_loc(self.now, method="nearest")
                        except Exception as e:
                            print(f"[Exception] {self.now} {symbol} {e}")
                            print(self.minute_history[symbol]["close"][-100:])
                            continue

                        price = self.minute_history[symbol]["close"][
                            minute_index
                        ]

                        if symbol not in trading_data.positions:
                            trading_data.positions[symbol] = 0

                        do, what = await strategy.run(
                            symbol,
                            True,
                            int(trading_data.positions[symbol]),
                            self.minute_history[symbol][: minute_index + 1],
                            self.now,
                            self.portfolio_value,
                            debug=False,  # type: ignore
                            backtesting=True,
                        )
                        if do:
                            if (
                                what["side"] == "buy"
                                and float(what["qty"]) > 0
                                or what["side"] == "sell"
                                and float(what["qty"]) < 0
                            ):
                                trading_data.positions[symbol] += int(
                                    float(what["qty"])
                                )
                                trading_data.buy_time[
                                    symbol
                                ] = self.now.replace(second=0, microsecond=0)
                            else:
                                trading_data.positions[symbol] -= int(
                                    float(what["qty"])
                                )

                            trading_data.last_used_strategy[symbol] = strategy

                            rc_msg.append(
                                f"[{self.now}][{strategy.name}] {what['side']} {what['qty']} of {symbol} @ {price}"
                            )
                            db_trade = NewTrade(
                                algo_run_id=strategy.algo_run.run_id,
                                symbol=symbol,
                                qty=int(float(what["qty"])),
                                operation=what["side"],
                                price=price,
                                indicators=trading_data.buy_indicators[symbol]
                                if what["side"] == "buy"
                                else trading_data.sell_indicators[symbol],
                            )

                            await db_trade.save(
                                config.db_conn_pool,
                                str(self.now.to_pydatetime()),
                                trading_data.stop_prices[symbol],
                                trading_data.target_prices[symbol],
                            )

                            if what["side"] == "buy":
                                await strategy.buy_callback(
                                    symbol, price, int(float(what["qty"]))
                                )
                                break
                            elif what["side"] == "sell":
                                await strategy.sell_callback(
                                    symbol, price, int(float(what["qty"]))
                                )
                                break
                except Exception as e:
                    print(f"[Exception] {self.now} {symbol} {e}")
                    traceback.print_exc()

            self.now += timedelta(minutes=1)

            return True, rc_msg
        else:
            return False, []

    async def liquidate(self):
        for symbol in trading_data.positions:
            if (
                trading_data.positions[symbol] != 0
                and trading_data.last_used_strategy[symbol].type
                == StrategyType.DAY_TRADE
            ):
                position = trading_data.positions[symbol]
                minute_index = self.minute_history[symbol][
                    "close"
                ].index.get_loc(self.now, method="nearest")
                price = self.minute_history[symbol]["close"][minute_index]
                tlog(f"[{self.end}]{symbol} liquidate {position} at {price}")
                db_trade = NewTrade(
                    algo_run_id=trading_data.last_used_strategy[symbol].algo_run.run_id,  # type: ignore
                    symbol=symbol,
                    qty=int(position) if int(position) > 0 else -int(position),
                    operation="sell" if position > 0 else "buy",
                    price=price,
                    indicators={"liquidate": 1},
                )
                await db_trade.save(
                    config.db_conn_pool, str(self.now.to_pydatetime())
                )
