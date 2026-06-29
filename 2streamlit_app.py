import json
import time
from datetime import datetime

import altair as alt
import pandas as pd
import requests
import streamlit as st

# Official data.gov.sg HDB resale flat prices dataset, Jan 2017 onwards
DATASET_ID = "d_8b84c4ee58e3cfc0ece0d773c8ca6abc"
DATASTORE_URL = "https://data.gov.sg/api/action/datastore_search"

st.set_page_config(
    page_title="Potong Pasir HDB Price Monitor",
    page_icon="🏠",
    layout="wide",
)


def _to_number(series: pd.Series) -> pd.Series:
    """Convert messy numeric text to number."""
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce")


@st.cache_data(ttl=6 * 60 * 60, show_spinner=False)
def fetch_records(q: str | None = None, filters: dict | None = None, max_pages: int = 20) -> pd.DataFrame:
    """
    Fetch records from data.gov.sg Datastore Search API.

    q is a full-text search term. filters can be exact field matches, e.g. {"town": "TOA PAYOH"}.
    max_pages avoids accidental huge downloads if the query is too broad.
    """
    all_records = []
    offset = 0
    limit = 5000

    for _ in range(max_pages):
        params = {
            "resource_id": DATASET_ID,
            "limit": limit,
            "offset": offset,
        }
        if q:
            params["q"] = q
        if filters:
            params["filters"] = json.dumps(filters)

        response = requests.get(DATASTORE_URL, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()

        if not payload.get("success"):
            raise RuntimeError(payload)

        result = payload.get("result", {})
        records = result.get("records", [])
        all_records.extend(records)

        total = int(result.get("total", 0))
        offset += limit
        if offset >= total or not records:
            break

        # Be polite to public API services.
        time.sleep(0.2)

    return pd.DataFrame(all_records)


@st.cache_data(ttl=6 * 60 * 60, show_spinner=False)
def load_potong_pasir_data(keywords: tuple[str, ...]) -> pd.DataFrame:
    """Load HDB resale transactions matching Potong Pasir street keywords."""
    frames = []

    for keyword in keywords:
        keyword = keyword.strip().upper()
        if not keyword:
            continue
        df = fetch_records(q=keyword)
        if not df.empty:
            frames.append(df)

    # Fallback: Potong Pasir appears under the broader Toa Payoh HDB town in many HDB listings.
    # If keyword search returns no results, search TOA PAYOH town, then filter locally by street keyword.
    if not frames:
        fallback = fetch_records(filters={"town": "TOA PAYOH"}, max_pages=20)
        frames.append(fallback)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True).drop_duplicates()

    # Standardise column names from the official dataset.
    expected = [
        "month",
        "town",
        "flat_type",
        "block",
        "street_name",
        "storey_range",
        "floor_area_sqm",
        "flat_model",
        "lease_commence_date",
        "remaining_lease",
        "resale_price",
    ]
    missing = [col for col in expected if col not in df.columns]
    if missing:
        st.warning(f"Missing expected columns from API: {missing}")

    # Local area filter. This keeps it specific to Potong Pasir instead of the whole Toa Payoh town.
    if "street_name" in df.columns:
        pattern = "|".join([k.strip().upper() for k in keywords if k.strip()])
        df = df[df["street_name"].astype(str).str.upper().str.contains(pattern, na=False, regex=True)]

    if df.empty:
        return df

    df["month_date"] = pd.to_datetime(df["month"], errors="coerce")
    df["resale_price"] = _to_number(df["resale_price"])
    df["floor_area_sqm"] = _to_number(df["floor_area_sqm"])
    df["floor_area_sqft"] = df["floor_area_sqm"] * 10.7639
    df["price_psm"] = df["resale_price"] / df["floor_area_sqm"]
    df["price_psf"] = df["resale_price"] / df["floor_area_sqft"]

    # Cleaner labels.
    for col in ["town", "flat_type", "street_name", "storey_range", "flat_model"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.upper().str.strip()

    return df.sort_values("month_date", ascending=False)


def pct_change(new: float, old: float) -> str:
    if pd.isna(new) or pd.isna(old) or old == 0:
        return "n/a"
    return f"{((new / old) - 1) * 100:+.1f}%"


st.title("🏠 Potong Pasir HDB Resale Price Monitor")
st.caption("Uses official HDB resale transactions from data.gov.sg. Prices are indicative and depend on unit attributes and buyer-seller negotiation.")

with st.sidebar:
    st.header("Filters")
    keyword_text = st.text_input(
        "Street keyword(s)",
        value="POTONG PASIR",
        help="Comma-separated. Example: POTONG PASIR, JOO SENG",
    )
    keywords = tuple([k.strip().upper() for k in keyword_text.split(",") if k.strip()])

    refresh = st.button("Refresh data now")
    if refresh:
        st.cache_data.clear()
        st.rerun()

with st.spinner("Loading HDB resale data..."):
    raw = load_potong_pasir_data(keywords)

if raw.empty:
    st.error("No HDB resale records found. Try another street keyword, e.g. POTONG PASIR or JOO SENG.")
    st.stop()

min_month = raw["month_date"].min().date()
max_month = raw["month_date"].max().date()

with st.sidebar:
    flat_options = sorted(raw["flat_type"].dropna().unique())
    selected_flat_types = st.multiselect("Flat type", flat_options, default=flat_options)

    streets = sorted(raw["street_name"].dropna().unique())
    selected_streets = st.multiselect("Street", streets, default=streets)

    date_range = st.slider(
        "Transaction period",
        min_value=min_month,
        max_value=max_month,
        value=(min_month, max_month),
    )

    min_price = int(raw["resale_price"].min())
    max_price = int(raw["resale_price"].max())
    price_range = st.slider(
        "Price range (S$)",
        min_value=min_price,
        max_value=max_price,
        value=(min_price, max_price),
        step=10000,
    )

    st.header("Simple alert")
    alert_price = st.number_input(
        "Warn if latest median exceeds S$",
        min_value=0,
        value=0,
        step=10000,
        help="This displays a warning inside the app. For email/Telegram alerts, use GitHub Actions or a scheduled job.",
    )

start_date, end_date = pd.to_datetime(date_range[0]), pd.to_datetime(date_range[1])
filtered = raw[
    raw["flat_type"].isin(selected_flat_types)
    & raw["street_name"].isin(selected_streets)
    & (raw["month_date"] >= start_date)
    & (raw["month_date"] <= end_date)
    & (raw["resale_price"].between(price_range[0], price_range[1]))
].copy()

if filtered.empty:
    st.warning("No transactions match the selected filters.")
    st.stop()

latest_month = filtered["month_date"].max()
latest_df = filtered[filtered["month_date"] == latest_month]
latest_median = latest_df["resale_price"].median()
latest_psf = latest_df["price_psf"].median()

monthly = (
    filtered.groupby("month_date", as_index=False)
    .agg(
        median_price=("resale_price", "median"),
        avg_price=("resale_price", "mean"),
        median_psf=("price_psf", "median"),
        transactions=("resale_price", "count"),
    )
    .sort_values("month_date")
)

last_3m = monthly.tail(3)["median_price"].mean()
prev_3m = monthly.iloc[-6:-3]["median_price"].mean() if len(monthly) >= 6 else float("nan")
last_12m = monthly.tail(12)["median_price"].mean()
prev_12m = monthly.iloc[-24:-12]["median_price"].mean() if len(monthly) >= 24 else float("nan")

kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
kpi1.metric("Latest month", latest_month.strftime("%b %Y"))
kpi2.metric("Latest median", f"S${latest_median:,.0f}")
kpi3.metric("Latest median PSF", f"S${latest_psf:,.0f}")
kpi4.metric("3M trend", pct_change(last_3m, prev_3m))
kpi5.metric("Transactions", f"{len(filtered):,}")

if alert_price and latest_median > alert_price:
    st.warning(f"Alert: latest median resale price is S${latest_median:,.0f}, above your threshold of S${alert_price:,.0f}.")

st.subheader("Median resale price trend")
trend_by_type = (
    filtered.groupby(["month_date", "flat_type"], as_index=False)
    .agg(median_price=("resale_price", "median"), transactions=("resale_price", "count"))
)
price_chart = (
    alt.Chart(trend_by_type)
    .mark_line(point=True)
    .encode(
        x=alt.X("month_date:T", title="Month"),
        y=alt.Y("median_price:Q", title="Median resale price (S$)", axis=alt.Axis(format="$,.0f")),
        color=alt.Color("flat_type:N", title="Flat type"),
        tooltip=[
            alt.Tooltip("month_date:T", title="Month", format="%b %Y"),
            alt.Tooltip("flat_type:N", title="Flat type"),
            alt.Tooltip("median_price:Q", title="Median price", format="$,.0f"),
            alt.Tooltip("transactions:Q", title="Transactions"),
        ],
    )
    .properties(height=420)
)
st.altair_chart(price_chart, use_container_width=True)

st.subheader("Median price per square foot trend")
psf_by_type = (
    filtered.groupby(["month_date", "flat_type"], as_index=False)
    .agg(median_psf=("price_psf", "median"), transactions=("resale_price", "count"))
)
psf_chart = (
    alt.Chart(psf_by_type)
    .mark_line(point=True)
    .encode(
        x=alt.X("month_date:T", title="Month"),
        y=alt.Y("median_psf:Q", title="Median price PSF (S$)", axis=alt.Axis(format="$,.0f")),
        color=alt.Color("flat_type:N", title="Flat type"),
        tooltip=[
            alt.Tooltip("month_date:T", title="Month", format="%b %Y"),
            alt.Tooltip("flat_type:N", title="Flat type"),
            alt.Tooltip("median_psf:Q", title="Median PSF", format="$,.0f"),
            alt.Tooltip("transactions:Q", title="Transactions"),
        ],
    )
    .properties(height=360)
)
st.altair_chart(psf_chart, use_container_width=True)

left, right = st.columns(2)

with left:
    st.subheader("Price by flat type")
    summary = (
        filtered.groupby("flat_type", as_index=False)
        .agg(
            transactions=("resale_price", "count"),
            median_price=("resale_price", "median"),
            avg_price=("resale_price", "mean"),
            median_psf=("price_psf", "median"),
            min_price=("resale_price", "min"),
            max_price=("resale_price", "max"),
        )
        .sort_values("median_price", ascending=False)
    )
    st.dataframe(
        summary,
        use_container_width=True,
        hide_index=True,
        column_config={
            "median_price": st.column_config.NumberColumn("Median price", format="S$%d"),
            "avg_price": st.column_config.NumberColumn("Average price", format="S$%d"),
            "median_psf": st.column_config.NumberColumn("Median PSF", format="S$%d"),
            "min_price": st.column_config.NumberColumn("Min", format="S$%d"),
            "max_price": st.column_config.NumberColumn("Max", format="S$%d"),
        },
    )

with right:
    st.subheader("Transaction volume")
    volume = filtered.groupby("month_date", as_index=False).size().rename(columns={"size": "transactions"})
    volume_chart = (
        alt.Chart(volume)
        .mark_bar()
        .encode(
            x=alt.X("month_date:T", title="Month"),
            y=alt.Y("transactions:Q", title="Transactions"),
            tooltip=[alt.Tooltip("month_date:T", title="Month", format="%b %Y"), "transactions"],
        )
        .properties(height=300)
    )
    st.altair_chart(volume_chart, use_container_width=True)

st.subheader("Latest transactions")
show_cols = [
    "month",
    "town",
    "flat_type",
    "block",
    "street_name",
    "storey_range",
    "floor_area_sqm",
    "flat_model",
    "remaining_lease",
    "resale_price",
    "price_psf",
]
show_cols = [col for col in show_cols if col in filtered.columns]
latest_table = filtered.sort_values(["month_date", "resale_price"], ascending=[False, False])[show_cols]
st.dataframe(
    latest_table,
    use_container_width=True,
    hide_index=True,
    column_config={
        "resale_price": st.column_config.NumberColumn("resale_price", format="S$%d"),
        "price_psf": st.column_config.NumberColumn("price_psf", format="S$%d"),
    },
)

csv = latest_table.to_csv(index=False).encode("utf-8")
st.download_button(
    "Download filtered transactions as CSV",
    data=csv,
    file_name="potong_pasir_hdb_resale_transactions.csv",
    mime="text/csv",
)

st.caption(
    f"Last app refresh: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}. "
    "Data source: data.gov.sg HDB resale flat prices dataset."
)
