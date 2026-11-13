"""Synthetic credit warehouse generator.

Produces a small but structurally realistic lending book so every topic type in
the design document can be demonstrated offline:

* ``dim_customer``      - customer master (product, channel, region, segment)
* ``fct_application``   - one row per application with decision + features
* ``fct_loan``          - one row per disbursement
* ``fct_repayment``     - disbursement performance (for overdue / roll-rate)
* ``dim_feature_snapshot`` - monthly feature snapshots (for PSI drift analysis)

A deliberate regime shift is injected in the final three months: 惠农贷
applications originating from 华中 degrade sharply, with 渠道C inside 华中
worse again. That is the root cause a 逾期异动诊断 topic is expected to
discover, and it is observable at region level (so a human-added
"区域维度下钻" task actually finds something).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

PRODUCTS = {
    "惠农贷": {"base_odds": 0.055, "ticket": 80_000},
    "消费贷": {"base_odds": 0.032, "ticket": 45_000},
    "经营贷": {"base_odds": 0.041, "ticket": 220_000},
}

CHANNELS = ["自营App", "渠道A", "渠道B", "渠道C", "线下网点"]
REGIONS = ["华东", "华南", "华中", "华北", "西南", "西北"]
SEGMENTS = ["新客", "存量复借", "存量增额", "流失召回"]
EDUCATION = ["高中及以下", "大专", "本科", "硕士及以上"]
OCCUPATION = ["农户", "个体工商户", "企业主", "工薪", "自由职业"]


@dataclass
class GeneratorConfig:
    seed: int = 20240501
    n_customers: int = 40_000
    n_applications: int = 150_000
    months: int = 18
    end_month: str = "2024-12"
    # --- injected anomaly ---
    anomaly_product: str = "惠农贷"
    anomaly_region: str = "华中"
    anomaly_channel: str = "渠道C"
    anomaly_months: int = 3
    anomaly_region_multiplier: float = 2.8  # 惠农贷 x 华中
    anomaly_channel_multiplier: float = 1.7  # ... and 渠道C inside it


def _month_range(end_month: str, months: int) -> List[str]:
    year, mon = int(end_month[:4]), int(end_month[5:7])
    out = []
    for i in range(months):
        m = mon - i
        y = year
        while m <= 0:
            m += 12
            y -= 1
        out.append(f"{y}-{m:02d}")
    return list(reversed(out))


def generate(config: Optional[GeneratorConfig] = None) -> Dict[str, pd.DataFrame]:
    """Build every warehouse table as an in-memory DataFrame."""
    cfg = config or GeneratorConfig()
    rng = np.random.default_rng(cfg.seed)
    months = _month_range(cfg.end_month, cfg.months)
    n_months = len(months)
    n_cust = cfg.n_customers
    n_app = cfg.n_applications
    anomaly_start_idx = n_months - cfg.anomaly_months

    # ---------------- customer master ----------------
    cust_ids = np.array([f"C{100000 + i}" for i in range(n_cust)], dtype=object)
    dim_customer = pd.DataFrame(
        {
            "cust_id": cust_ids,
            "product": rng.choice(list(PRODUCTS), size=n_cust, p=[0.34, 0.42, 0.24]),
            "channel": rng.choice(CHANNELS, size=n_cust, p=[0.28, 0.22, 0.18, 0.16, 0.16]),
            "region": rng.choice(REGIONS, size=n_cust, p=[0.26, 0.20, 0.18, 0.16, 0.12, 0.08]),
            "segment": rng.choice(SEGMENTS, size=n_cust, p=[0.40, 0.34, 0.16, 0.10]),
            "age": np.clip(rng.normal(38, 9, n_cust).astype(int), 20, 65),
            "gender": rng.choice(["男", "女"], size=n_cust, p=[0.62, 0.38]),
            "education": rng.choice(EDUCATION, size=n_cust, p=[0.30, 0.30, 0.30, 0.10]),
            "occupation": rng.choice(OCCUPATION, size=n_cust, p=[0.30, 0.24, 0.12, 0.26, 0.08]),
            "marriage": rng.choice(["未婚", "已婚", "离异"], size=n_cust, p=[0.22, 0.68, 0.10]),
        }
    )

    # ---------------- applications (vectorised) ----------------
    cust_idx = rng.integers(0, n_cust, size=n_app)
    month_idx = rng.integers(0, n_months, size=n_app)

    prod = dim_customer["product"].to_numpy()[cust_idx]
    chan = dim_customer["channel"].to_numpy()[cust_idx]
    reg = dim_customer["region"].to_numpy()[cust_idx]
    seg = dim_customer["segment"].to_numpy()[cust_idx]
    age = dim_customer["age"].to_numpy()[cust_idx]

    credit_score = np.clip(rng.normal(650, 70, n_app), 350, 850).astype(int)
    dti = np.clip(rng.gamma(2.0, 0.16, n_app), 0.02, 1.6)
    query_3m = rng.poisson(1.6, n_app)
    overdue_hist = rng.poisson(0.25, n_app)
    income = np.clip(rng.lognormal(11.0, 0.55, n_app), 30_000, 900_000)
    utilisation = np.clip(rng.beta(2.2, 2.4, n_app), 0.01, 1.0)
    tenure = rng.integers(1, 84, n_app)

    base_odds = np.array([PRODUCTS[p]["base_odds"] for p in prod])
    ticket = np.array([PRODUCTS[p]["ticket"] for p in prod])

    odds = base_odds.copy()
    odds *= 1.0 + np.maximum(0.0, (650 - credit_score) / 650) * 2.6
    odds *= 1.0 + np.maximum(0.0, dti - 0.4) * 1.7
    odds *= 1.0 + query_3m * 0.12
    odds *= 1.0 + overdue_hist * 0.35
    odds *= 1.0 + utilisation * 0.30
    odds *= np.where(chan == cfg.anomaly_channel, 1.08, 1.0)
    odds_pre_anomaly = np.clip(odds, 0.002, 0.75)

    # ---- regime shift: last N months, 惠农贷 x 华中 (x 渠道C again) ----
    in_window = month_idx >= anomaly_start_idx
    is_anom_prod = prod == cfg.anomaly_product
    is_anom_reg = reg == cfg.anomaly_region
    is_anom_chan = chan == cfg.anomaly_channel
    odds *= np.where(in_window & is_anom_prod & is_anom_reg, cfg.anomaly_region_multiplier, 1.0)
    odds *= np.where(in_window & is_anom_prod & is_anom_reg & is_anom_chan, cfg.anomaly_channel_multiplier, 1.0)
    odds = np.clip(odds, 0.002, 0.75)

    # The screening policy runs on the *pre-shift* odds: a real scorecard does
    # not react to a regime change the month it starts, which is precisely why
    # the degraded cohort still gets booked and shows up as overdue later.
    reject_score = np.clip(odds_pre_anomaly * 6.5, 0.05, 0.85)
    approved = (rng.random(n_app) > reject_score).astype(int)
    amount = np.where(
        approved == 1,
        np.clip(ticket * rng.lognormal(0, 0.35, n_app), 5_000, 600_000),
        0.0,
    )

    fct_application = pd.DataFrame(
        {
            "apply_id": np.array([f"A{2000000 + i}" for i in range(n_app)], dtype=object),
            "cust_id": cust_ids[cust_idx],
            "apply_month": np.array([months[i] for i in month_idx], dtype=object),
            "product": prod,
            "channel": chan,
            "region": reg,
            "segment": seg,
            "age": age,
            "apply_amount": np.round(amount, 2),
            "approved": approved,
            "credit_score": credit_score,
            "dti": np.round(dti, 4),
            "query_cnt_3m": query_3m,
            "overdue_cnt_hist": overdue_hist,
            "income": np.round(income, 2),
            "credit_utilisation": np.round(utilisation, 4),
            "tenure_months": tenure,
        }
    )

    # ---------------- loans ----------------
    ap = fct_application[fct_application["approved"] == 1].reset_index(drop=True)
    n_loan = len(ap)
    fct_loan = pd.DataFrame(
        {
            "loan_id": np.array([f"L{3000000 + i}" for i in range(n_loan)], dtype=object),
            "apply_id": ap["apply_id"].to_numpy(),
            "cust_id": ap["cust_id"].to_numpy(),
            "product": ap["product"].to_numpy(),
            "channel": ap["channel"].to_numpy(),
            "region": ap["region"].to_numpy(),
            "loan_month": ap["apply_month"].to_numpy(),
            "principal": ap["apply_amount"].to_numpy(),
            "term_months": rng.choice([6, 12, 24, 36], size=n_loan, p=[0.18, 0.40, 0.28, 0.14]),
            "annual_rate": np.round(np.clip(rng.normal(0.098, 0.021, n_loan), 0.045, 0.24), 4),
        }
    )

    # ---------------- repayment performance ----------------
    loan_month_idx = np.array([months.index(m) for m in fct_loan["loan_month"]])
    l_prod = fct_loan["product"].to_numpy()
    l_reg = fct_loan["region"].to_numpy()
    l_chan = fct_loan["channel"].to_numpy()
    l_score = ap["credit_score"].to_numpy()
    l_dti = ap["dti"].to_numpy()

    pd_bad = 0.020 + np.maximum(0.0, (650 - l_score) / 650) * 0.20 + np.maximum(0.0, l_dti - 0.40) * 0.16
    l_in_window = loan_month_idx >= anomaly_start_idx
    pd_bad *= np.where(
        l_in_window & (l_prod == cfg.anomaly_product) & (l_reg == cfg.anomaly_region),
        cfg.anomaly_region_multiplier,
        1.0,
    )
    pd_bad *= np.where(
        l_in_window & (l_prod == cfg.anomaly_product) & (l_reg == cfg.anomaly_region) & (l_chan == cfg.anomaly_channel),
        cfg.anomaly_channel_multiplier,
        1.0,
    )
    pd_bad = np.clip(pd_bad, 0.001, 0.85)

    bad = (rng.random(n_loan) < pd_bad).astype(int)
    max_dpd = np.where(bad == 1, rng.integers(31, 150, n_loan), rng.integers(1, 30, n_loan))

    fct_repayment = pd.DataFrame(
        {
            "loan_id": fct_loan["loan_id"].to_numpy(),
            "cust_id": fct_loan["cust_id"].to_numpy(),
            "product": l_prod,
            "channel": l_chan,
            "region": l_reg,
            "segment": ap["segment"].to_numpy(),
            "loan_month": fct_loan["loan_month"].to_numpy(),
            "mob": np.full(n_loan, 6),
            "principal": fct_loan["principal"].to_numpy(),
            "max_dpd": max_dpd,
            "bad_flag": bad,
            "pd_score": np.round(np.clip(rng.normal(pd_bad, pd_bad * 0.25), 0.001, 0.95), 4),
        }
    )

    # ---------------- monthly feature snapshots (PSI material) ----------------
    snap_rows: List[Dict[str, Any]] = []
    for mi, month in enumerate(months):
        sample = fct_application[fct_application["apply_month"] == month]
        if sample.empty:
            continue
        for p in PRODUCTS:
            sub = sample[sample["product"] == p]
            if sub.empty:
                continue
            drift = 22.0 if (mi >= anomaly_start_idx and p == cfg.anomaly_product) else 0.0
            snap_rows.append(
                {
                    "snapshot_month": month,
                    "product": p,
                    "sample_cnt": int(len(sub)),
                    "credit_score_mean": round(float(sub["credit_score"].mean()) - drift, 3),
                    "credit_score_std": round(float(sub["credit_score"].std()), 3),
                    "dti_mean": round(float(sub["dti"].mean()) + (0.05 if drift else 0.0), 4),
                    "income_mean": round(float(sub["income"].mean()), 2),
                    "query_cnt_3m_mean": round(float(sub["query_cnt_3m"].mean()), 4),
                    "utilisation_mean": round(float(sub["credit_utilisation"].mean()), 4),
                    "approval_rate": round(float(sub["approved"].mean()), 4),
                }
            )
    dim_feature_snapshot = pd.DataFrame(snap_rows)

    return {
        "dim_customer": dim_customer,
        "fct_application": fct_application,
        "fct_loan": fct_loan,
        "fct_repayment": fct_repayment,
        "dim_feature_snapshot": dim_feature_snapshot,
    }


def _duckdb_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce pandas 3.x extension dtypes into ones DuckDB can ingest.

    pandas 3 defaults to a dedicated string dtype, and ``numpy.str_`` scalars
    (which ``rng.choice`` produces) are a ``str`` subclass that DuckDB's pandas
    scan refuses. Forcing plain Python ``str`` on every value fixes both.
    """
    out = df.copy()
    for col in out.columns:
        dtype = out[col].dtype
        if pd.api.types.is_string_dtype(dtype) or dtype == object:
            out[col] = pd.Series(
                [None if v is None or v is pd.NA else str(v) for v in out[col].tolist()],
                dtype=object,
                index=out.index,
            )
        elif str(dtype).startswith(("Int", "UInt", "Float", "boolean")):
            try:
                out[col] = out[col].astype(object).where(out[col].notna(), None)
            except (TypeError, ValueError):
                pass
    return out


def build_duckdb(target: Path, config: Optional[GeneratorConfig] = None) -> Path:
    """Generate the dataset and materialise it as a DuckDB database file.

    Uses ``CREATE OR REPLACE`` so an existing file is updated in place rather
    than deleted - a stale WAL beside a removed database file would otherwise
    invalidate the rebuild.
    """
    import duckdb

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    frames = generate(config)
    con = duckdb.connect(str(target))
    try:
        for name, df in frames.items():
            con.register("_tmp", _duckdb_safe(df))
            con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _tmp")
            con.unregister("_tmp")
        con.execute(
            """CREATE OR REPLACE VIEW vw_overdue_by_month AS
               SELECT r.loan_month,
                      r.product,
                      r.channel,
                      r.region,
                      r.segment,
                      COUNT(*)                        AS loan_cnt,
                      SUM(r.principal)                AS principal_amt,
                      SUM(CASE WHEN r.bad_flag = 1 THEN 1 ELSE 0 END)           AS bad_cnt,
                      SUM(CASE WHEN r.bad_flag = 1 THEN r.principal ELSE 0 END) AS bad_principal
               FROM fct_repayment r
               GROUP BY 1,2,3,4,5"""
        )
        con.execute(
            """CREATE OR REPLACE VIEW vw_approval_by_month AS
               SELECT apply_month, product, channel, region,
                      COUNT(*)          AS apply_cnt,
                      SUM(approved)     AS approved_cnt,
                      AVG(credit_score) AS avg_credit_score,
                      AVG(dti)          AS avg_dti
               FROM fct_application
               GROUP BY 1,2,3,4"""
        )
    finally:
        con.close()
    return target
