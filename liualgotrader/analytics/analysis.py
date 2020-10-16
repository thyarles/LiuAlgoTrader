from datetime import date, timedelta

import pandas as pd
from sqlalchemy import create_engine

from liualgotrader.common import config
from liualgotrader.common.tlog import tlog

try:
    db_conn = create_engine(config.dsn)
except Exception as e:
    tlog(f"[EXCEPTION] {config.dsn} : {e}")


def load_trades(day: date, env: str) -> pd.DataFrame:
    query = f"""
    SELECT t.*, a.batch_id
    FROM 
    new_trades as t, algo_run as a
    WHERE 
        t.algo_run_id = a.algo_run_id AND 
        t.tstamp >= '{day}' AND 
        t.tstamp < '{day + timedelta(days=1)}'AND
        t.expire_tstamp is null AND
        a.algo_env = '{env}'
    ORDER BY symbol, tstamp
    """
    return pd.read_sql_query(query, db_conn)


def load_trades_by_batch_id(batch_id: str) -> pd.DataFrame:
    query = f"""
    SELECT t.*, a.batch_id 
    FROM 
    new_trades as t, algo_run as a
    WHERE 
        t.algo_run_id = a.algo_run_id AND 
        a.batch_id = '{batch_id}' AND
        t.expire_tstamp is null
    ORDER BY symbol, tstamp
    """
    df = pd.read_sql_query(query, db_conn)
    try:
        df["client_time"] = pd.to_datetime(df["client_time"])
    except Exception:
        tlog(
            f"[Error] load_trades_by_batch_id({batch_id}) can't convert 'client_time' column to datetime"
        )

    return df


def load_runs(day: date, env: str) -> pd.DataFrame:
    query = f"""
     SELECT * 
     FROM 
     algo_run as t
     WHERE 
         start_time >= '{day}' AND 
         start_time < '{day + timedelta(days=1)}' AND
         algo_env = '{env}'
     ORDER BY start_time
     """
    df = pd.read_sql_query(query, db_conn)
    df.set_index("algo_run_id", inplace=True)
    return df


def load_batch_list(day: date, env: str) -> pd.DataFrame:
    query = f"""
        SELECT DISTINCT a.batch_id 
        FROM 
        new_trades as t, algo_run as a
        WHERE 
            t.algo_run_id = a.algo_run_id AND 
            t.tstamp >= '{day}' AND 
            t.tstamp < '{day + timedelta(days=1)}'AND
            t.expire_tstamp is null AND
            a.algo_env = '{env}'
        """
    return pd.read_sql_query(query, db_conn)


def load_batch_symbols(batch_id: str) -> pd.DataFrame:
    query = f"""
        SELECT 
            symbol
        FROM 
            trending_tickers
        WHERE 
            batch_id = '{batch_id}'
    """
    return pd.read_sql_query(query, db_conn)


def calc_batch_revenue(
    symbol: str, trades: pd.DataFrame, batch_id: str
) -> float:
    symbol_df = trades[
        (trades["symbol"] == symbol) & (trades["batch_id"] == batch_id)
    ]
    rc = 0
    for index, row in symbol_df.iterrows():
        rc += (
            row["price"]
            * row["qty"]
            * (1 if row["operation"] == "sell" and row["qty"] > 0 else -1)
        )
    return round(rc, 2)


def calc_revenue(symbol: str, trades: pd.DataFrame, env) -> float:
    symbol_df = trades[
        (trades["symbol"] == symbol) & (trades["algo_env"] == env)
    ]
    rc = 0
    for index, row in symbol_df.iterrows():
        rc += (
            row["price"]
            * row["qty"]
            * (1 if row["operation"] == "sell" and row["qty"] > 0 else -1)
        )
    return round(rc, 2)


def count_trades(symbol, trades: pd.DataFrame, batch_id: str) -> int:
    symbol_df = trades[
        (trades["symbol"] == symbol) & (trades["batch_id"] == batch_id)
    ]
    return len(symbol_df.index)
