import base64
import json
import logging
import os
from pathlib import Path
import sys
from logging import FileHandler, Logger, StreamHandler
from multiprocessing import cpu_count

import sentry_sdk
import pandas_market_calendars as mcal
import uvloop
from sentry_sdk import capture_exception
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker


sentry_sdk.init(
    dsn="https://e76d761b19864956a5a95476a7a41f6a@o4504712959557632.ingest.sentry.io/4504782774337536",
    traces_sample_rate=0.1,
)

ENVIRONMENT = os.environ.get("ENVIRONMENT")
DEBUG = False


# NOTE: use this function if pass variables to env via docker .env file. Otherwise use .pgpass
def decode_env_var(env_var_name: str) -> dict:
    if ENVIRONMENT == "LOCAL":
        env_var_dict = {env_var_name: os.environ.get(env_var_name)}
    else:
        env_var_dict = json.loads(base64.b64decode(os.environ.get(env_var_name, "")).decode("utf-8"))
    return env_var_dict


def db_uri_maker() -> str:
    """requires a PGPASSFILE env variable directing to a .pgpass file"""
    if ENVIRONMENT == "LOCAL":
        with open(os.environ.get("PGPASSFILE"), "r") as f:
            host, port, dbname, user, password = f.read().strip().split(":")
            database_uri = f"postgresql+psycopg://{user}:{password}@{host}:{port}/{dbname}"
    else:
        database_uri = "postgresql+psycopg://{username}:{password}@{host}:{port}/{database}".format(
            **decode_env_var("postgres")
        )
    return database_uri


POSTGRES_DATABASE_URL = db_uri_maker()
POSTGRES_BATCH_MAX = 62000

BASE_DOWNLOAD_PATH = str(Path("~").expanduser()) + "/.polygon_data"

POLYGON_BASE_URL = "https://api.polygon.io"
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")
RH_USERNAME = os.getenv("RH_USERNAME")
RH_PASSWORD = os.getenv("RH_PASSWORD")
QR = os.getenv("RH_QR")
TRAD_IRA = os.getenv("TRAD_IRA")
ROTH_IRA = os.getenv("ROTH_IRA")

# total number of requests the API can handle at once. 100/sec rate limit
MAX_CONCURRENT_REQUESTS = 250
# Max number of requests per minute for the free API tier: 5
MAX_QUERY_PER_MINUTE = 4

CPUS = cpu_count() - 2

POOL_DEFAULT_KWARGS = {
    "processes": CPUS,
    "exception_handler": capture_exception,
    "loop_initializer": uvloop.new_event_loop,
    "childconcurrency": int(MAX_CONCURRENT_REQUESTS / CPUS),
    "queuecount": CPUS,
}

async_engine = create_async_engine(
    POSTGRES_DATABASE_URL,
    future=True,
    echo=False,
    echo_pool=True,
    max_overflow=40,
    pool_pre_ping=True,
    pool_size=50,  # half of the available 100 connections
    pool_recycle=180,
    pool_timeout=30,
)

async_session_maker = sessionmaker(
    async_engine,
    autoflush=False,
    autocommit=False,
    class_=AsyncSession,
    expire_on_commit=False,
    future=True,
)

root_logger: Logger = logging.getLogger()
log: Logger = logging.getLogger(__name__)
log_formatter = logging.Formatter(
    "%(asctime)s - "
    "%(levelname)s - "
    "%(processName)s:%(threadName)s - "
    "%(filename)s:%(funcName)s:%(lineno)d - "
    "%(message)s - "
    "%(context)s"
)


class ContextFilter(logging.Filter):
    """This is a filter which injects contextual information into the log."""

    def filter(self, record):
        if not hasattr(record, "context"):
            record.context = ""
        return True


# Add filter to give default context value of ""
context_filter = ContextFilter()
log.addFilter(context_filter)

# Remove all existing log handlers
for handler in root_logger.handlers:
    root_logger.removeHandler(handler)
for handler in log.handlers:
    log.removeHandler(handler)

# Set logging level based on env var
if DEBUG:
    log.setLevel(logging.DEBUG)
else:
    log.setLevel(logging.INFO)

# Add a stream handler to stdout
stream_handler = StreamHandler(sys.stdout)
stream_handler.setFormatter(log_formatter)

# Add a filehandler
file_handler = FileHandler(".logs/option_bot_logs.log")
file_handler.setFormatter(log_formatter)

log.addHandler(stream_handler)
log.addHandler(file_handler)


# market calendar
o_cal = mcal.get_calendar("CBOE_Equity_Options")
o_cal = o_cal.schedule(start_date="2021-01-01", end_date="2025-01-01")

e_cal = mcal.get_calendar("NYSE")
e_cal = e_cal.schedule(start_date="2021-01-01", end_date="2025-01-01")
