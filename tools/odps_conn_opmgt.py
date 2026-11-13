#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ODPS (MaxCompute) connection helper — project ``opmgt_dev``.

Security
--------
Credentials are **never** hard-coded. They are read from the environment (or a
local, git-ignored ``.env``), so nothing sensitive ever reaches version control::

    CREDIT_AGENT_ODPS_ACCESS_ID
    CREDIT_AGENT_ODPS_ACCESS_KEY
    CREDIT_AGENT_ODPS_ENDPOINT   # optional, has a default
    CREDIT_AGENT_ODPS_PROJECT    # defaults to "opmgt_dev"

Copy ``.env.example`` to ``.env`` and fill it in. The ODPS client is built
lazily, so importing this module without credentials is fine.

@File : odps_conn_opmgt.py
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

DEFAULT_ENDPOINT = "http://service.cn.maxcompute.aliyun.com/api"
DEFAULT_PROJECT = "opmgt_dev"

_ENV_FILE_HINT = ".env"


def _load_dotenv_if_present() -> None:
    """Populate ``os.environ`` from a root ``.env`` without overriding real vars.

    Dependency-free on purpose (no python-dotenv requirement) and best effort:
    a missing or malformed file is not an error.
    """
    for base in (Path(__file__).resolve().parents[1], Path.cwd()):
        path = base / _ENV_FILE_HINT
        if not path.is_file():
            continue
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip().removeprefix("export ").strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            continue
        return


_load_dotenv_if_present()


class CredentialsError(RuntimeError):
    """Raised when ODPS credentials are missing or incomplete."""


def get_credentials() -> Dict[str, str]:
    """Read ODPS credentials from the environment.

    Raises:
        CredentialsError: naming the exact variables that need setting.
    """
    access_id = os.environ.get("CREDIT_AGENT_ODPS_ACCESS_ID", "").strip()
    access_key = os.environ.get("CREDIT_AGENT_ODPS_ACCESS_KEY", "").strip()
    endpoint = os.environ.get("CREDIT_AGENT_ODPS_ENDPOINT", "").strip() or DEFAULT_ENDPOINT
    project = os.environ.get("CREDIT_AGENT_ODPS_PROJECT", "").strip() or DEFAULT_PROJECT

    missing = [
        name
        for name, value in (
            ("CREDIT_AGENT_ODPS_ACCESS_ID", access_id),
            ("CREDIT_AGENT_ODPS_ACCESS_KEY", access_key),
        )
        if not value
    ]
    if missing:
        raise CredentialsError(
            "missing ODPS credentials: "
            + ", ".join(missing)
            + " — copy .env.example to .env and fill it in (never commit it)"
        )
    return {
        "access_id": access_id,
        "secret_access_key": access_key,
        "endpoint": endpoint,
        "project": project,
    }


_o_cache: Optional[Any] = None


def get_odps() -> Any:
    """Lazily build the ODPS client, so import never fails on missing secrets."""
    global _o_cache
    if _o_cache is None:
        from odps import ODPS  # imported here to keep import cheap

        _o_cache = ODPS(**get_credentials())
    return _o_cache


def __getattr__(name: str) -> Any:
    """Keep ``from odps_conn_opmgt import o`` working while staying lazy."""
    if name == "o":
        return get_odps()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Part0: 数据读取函数
def odps_convert(sql: str):
    """执行 SQL 并返回 ODPS Instance（平台侧已做只读校验）。"""
    return get_odps().execute_sql(sql)


def Instance_convert_df(data) -> pd.DataFrame:
    """把 ODPS Instance 的读取结果转成 DataFrame。"""
    rows: List[Any] = []
    columns: List[str] = []
    with data.open_reader() as reader:
        for line in reader:
            columns = [c.name for c in line._columns]
            rows.append(line._values)
    return pd.DataFrame(rows, columns=columns)


def query_df(sql: str, *, limit: Optional[int] = None) -> pd.DataFrame:
    """Convenience wrapper: SQL -> DataFrame in one call."""
    df = Instance_convert_df(odps_convert(sql))
    if limit and len(df) > limit:
        return df.head(limit)
    return df


def get_data(data_path, file_name, is_online=False, sql_str=None, is_save=True):
    """读取本地 CSV，或（is_online=True）用 SQL 从 ODPS 取数并可选落盘。"""
    file = os.path.join(data_path, file_name)
    if is_online:
        if sql_str is not None:
            df_data = query_df(sql_str)
            if is_save:
                Path(data_path).mkdir(parents=True, exist_ok=True)
                df_data.to_csv(file, encoding="utf-8", index=None)
            return df_data
        print("请输入查询SQL")
        return None
    if file_name is not None:
        return pd.read_csv(file)
    print("请输入文件名")
    return None


def upload_data(py_df, table_name, schema=None, is_partition=False, lifecycle=30,
                pt=None):
    """把 DataFrame 上传到 ODPS 表（需要写权限，Agent 链路不会调用）。"""
    from datetime import datetime, timedelta

    from odps.df import DataFrame

    if pt is None:
        pt = f"pt={(datetime.today().date() + timedelta(days=-1)).strftime('%Y%m%d')}"

    odps_df = DataFrame(py_df, unknown_as_string=True, as_type=schema)
    if is_partition:
        odps_df.persist(table_name, partition=pt, odps=get_odps(),
                        lifecycle=lifecycle, create_partition=True)
    else:
        odps_df.persist(table_name, odps=get_odps(), lifecycle=lifecycle)
    print("上传完成")


def upload_data2(py_df, table_name, pt):
    """动态插入分区（分区列需为整型）。"""
    from odps.df import DataFrame

    odps_df = DataFrame(py_df, unknown_as_string=True)
    odps_df.persist(table_name, partitions=pt, odps=get_odps())
    print("上传完成")


# 创建表
def gmt_table(tb_name, df, is_partition=False, schema_name=None,
              schema_type=None, schema_comment=None):
    if schema_name is None:
        schema_name = df.columns.values.tolist()
    if schema_type is None:
        schema_type = ["string"] * len(schema_name)
    if schema_comment is None:
        schema_comment = [""] * len(schema_name)

    from odps.models import Column, Partition, Schema

    df_schema = pd.DataFrame(
        {"name": schema_name, "type": schema_type, "comment": schema_comment}
    )
    df_schema = df_schema[["name", "type", "comment"]]
    df_columns = df_schema.apply(lambda x: [Column(name=x[0], type=x[1], comment=x[2])], axis=1)

    columns: list = []
    for col in df_columns:
        columns = columns + col
    partitions = [Partition(name="pt", type="string", comment="")] if is_partition else None
    schema = Schema(columns=columns, partitions=partitions)
    table = get_odps().create_table(tb_name, schema=schema, if_not_exists=True)
    print("创建成功")
    return True
