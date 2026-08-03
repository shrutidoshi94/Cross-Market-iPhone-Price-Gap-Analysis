"""
Streamlit dashboard for Cross-Market iPhone Price Gap Analysis.

Visualizes USD-normalized prices over time, gaps vs. the US baseline,
and a simple "best market right now" callout based on arbitrage_score.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

from config import COUNTRY_CODES, PRODUCT_QUERY
from features import (
    DUTY_ESTIMATES,
    SHIPPING_ESTIMATES_USD,
    build_features,
    latest_snapshot,
)

st.set_page_config(
    page_title="iPhone Price Gap Analysis",
    layout="wide",
)

# Country display labels for the sidebar / charts
COUNTRY_LABELS = {
    "US": "United States",
    "GB": "United Kingdom",
    "DE": "Germany",
    "FR": "France",
    "CA": "Canada",
    "AU": "Australia",
    "JP": "Japan",
    "IN": "India",
}


@st.cache_data(ttl=60)
def load_features() -> pd.DataFrame:
    """Load engineered features; cache briefly so the UI stays snappy."""
    return build_features()


def _format_money(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"${value:,.2f}"


def _format_pct(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value * 100:+.1f}%"


def render_header(df: pd.DataFrame) -> None:
    """Product title, optional image, and last-updated timestamp."""
    title = PRODUCT_QUERY
    image_url = None
    last_updated = None

    if not df.empty:
        latest_row = df.sort_values("timestamp").iloc[-1]
        if pd.notna(latest_row.get("title")):
            title = str(latest_row["title"])
        last_updated = latest_row["timestamp"]
        # image_url is optional — present only if collect.py stored it
        if "image_url" in df.columns and pd.notna(latest_row.get("image_url")):
            image_url = latest_row["image_url"]

    left, right = st.columns([3, 1])
    with left:
        st.title(title)
        st.caption(
            f"Tracked SKU: **{PRODUCT_QUERY}** only · "
            "Other storages/models and outlier prices are filtered out · "
            "USD-normalized"
        )
        if last_updated is not None:
            # pandas Timestamp → readable UTC string
            stamp = pd.Timestamp(last_updated).strftime("%Y-%m-%d %H:%M UTC")
            st.caption(f"Last updated: {stamp}")
        else:
            st.caption("Last updated: no snapshots yet")
    with right:
        if image_url:
            st.image(image_url, use_container_width=True)


def render_best_market(latest: pd.DataFrame) -> None:
    """Highlighted callout for the best market by arbitrage_score."""
    st.subheader("Best market right now")

    if latest.empty or latest["arbitrage_score"].isna().all():
        st.info("Not enough data yet to pick a best market.")
        return

    ranked = latest.dropna(subset=["arbitrage_score"]).sort_values(
        "arbitrage_score", ascending=False
    )
    best = ranked.iloc[0]
    country = best["country"]
    label = COUNTRY_LABELS.get(country, country)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Best market", label)
    c2.metric("Price (USD)", _format_money(best["price_usd"]))
    c3.metric("Gap vs US", _format_money(best["price_gap_usd"]))
    c4.metric("Arbitrage score", f"{best['arbitrage_score']:,.1f}")

    st.caption(
        "Arbitrage score = how much cheaper the market is vs. the US, "
        "minus rough shipping + duty estimates. Higher is better for a "
        "US-based buyer. See the disclaimer below."
    )


def render_price_chart(df: pd.DataFrame, countries: list[str]) -> None:
    """Plotly line chart of price_usd over time for selected countries."""
    st.subheader("Price over time (USD)")

    subset = df[df["country"].isin(countries)].dropna(subset=["price_usd"])
    if subset.empty:
        st.warning("No price history for the selected countries yet.")
        return

    chart_df = subset.copy()
    chart_df["market"] = chart_df["country"].map(
        lambda c: COUNTRY_LABELS.get(c, c)
    )

    fig = px.line(
        chart_df,
        x="timestamp",
        y="price_usd",
        color="market",
        markers=True,
        labels={
            "timestamp": "Time (UTC)",
            "price_usd": "Price (USD)",
            "market": "Market",
        },
    )
    fig.update_layout(
        hovermode="x unified",
        legend_title_text="Market",
        margin=dict(l=10, r=10, t=30, b=10),
        height=420,
    )
    st.plotly_chart(fig, use_container_width=True)


def render_gap_table(latest: pd.DataFrame, countries: list[str]) -> None:
    """Current snapshot table sorted cheapest → most expensive."""
    st.subheader("Current price gap vs. US")

    subset = latest[latest["country"].isin(countries)].copy()
    if subset.empty:
        st.warning("No current snapshot for the selected countries.")
        return

    subset = subset.sort_values("price_usd", ascending=True)
    table = pd.DataFrame(
        {
            "Market": subset["country"].map(lambda c: COUNTRY_LABELS.get(c, c)),
            "Country": subset["country"],
            "Retailer": subset["retailer"],
            "Title": subset["title"],
            "Local price": subset.apply(
                lambda r: f"{r['price']:,.2f} {r['currency']}"
                if pd.notna(r["price"])
                else "—",
                axis=1,
            ),
            "Price (USD)": subset["price_usd"].map(_format_money),
            "Gap vs US ($)": subset["price_gap_usd"].map(_format_money),
            "Gap vs US (%)": subset["price_gap_pct"].map(_format_pct),
            "Arbitrage score": subset["arbitrage_score"].map(
                lambda v: f"{v:,.1f}" if pd.notna(v) else "—"
            ),
            "Source page": subset["source_url"]
            if "source_url" in subset.columns
            else None,
        }
    )
    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Source page": st.column_config.LinkColumn(
                "Source page",
                help="Open the retailer listing to verify the SKU and price",
                display_text="Open listing",
            ),
            "Title": st.column_config.TextColumn(
                "Title",
                help="Must be iPhone 17 Pro Max 512GB",
                width="medium",
            ),
        },
    )


def render_volatility(df: pd.DataFrame, countries: list[str]) -> None:
    """Bar chart of price standard deviation per country."""
    st.subheader("Price volatility")
    st.caption("Standard deviation of USD price over the collection period.")

    subset = df[df["country"].isin(countries)].dropna(subset=["price_usd"])
    if subset.empty:
        st.warning("Not enough data to compute volatility.")
        return

    vol = (
        subset.groupby("country", as_index=False)["price_usd"]
        .std(ddof=0)
        .rename(columns={"price_usd": "stdev_usd"})
    )
    # With a single observation, stdev is 0 — still useful to show
    vol["stdev_usd"] = vol["stdev_usd"].fillna(0.0)
    vol["market"] = vol["country"].map(lambda c: COUNTRY_LABELS.get(c, c))
    vol = vol.sort_values("stdev_usd", ascending=False)

    fig = px.bar(
        vol,
        x="market",
        y="stdev_usd",
        labels={"market": "Market", "stdev_usd": "Std. dev. (USD)"},
        text_auto=".2f",
    )
    fig.update_layout(
        margin=dict(l=10, r=10, t=30, b=10),
        height=360,
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)


def render_disclaimer() -> None:
    """VAT / duty caveat with pointer to README methodology."""
    with st.expander("Disclaimer & methodology notes", expanded=True):
        st.markdown(
            """
**Prices may not be directly comparable across markets.**

- Retail prices can **include or exclude VAT/sales tax** depending on the
  country and retailer. A lower shelf price abroad is not always a real saving.
- **Duty and shipping estimates are rough assumptions**, not customs quotes.
  They live in `features.py` as `DUTY_ESTIMATES` and `SHIPPING_ESTIMATES_USD`
  (e.g. India duty friction ≈ 20% of price + $60 shipping).
- History is **self-collected** via hourly snapshots — PricesAPI.io returns
  current matches, not a long historical series out of the box.
- FX conversion uses daily rates from [exchangerate.host](https://exchangerate.host).

See the project **README** for how these assumptions were chosen and how to
re-tune them.
            """
        )

        duty_df = pd.DataFrame(
            [
                {
                    "Country": code,
                    "Duty / tax friction": f"{DUTY_ESTIMATES.get(code, 0):.0%}",
                    "Shipping estimate (USD)": SHIPPING_ESTIMATES_USD.get(code, "—"),
                }
                for code in COUNTRY_CODES
            ]
        )
        st.caption("Current friction assumptions used in arbitrage_score:")
        st.dataframe(duty_df, use_container_width=True, hide_index=True)


def main() -> None:
    df = load_features()
    render_header(df)

    if df.empty:
        st.warning(
            "No price snapshots in the database yet. "
            "Run `python collect.py --once` or `python scheduler.py --once` first."
        )
        render_disclaimer()
        return

    available = sorted(df["country"].unique().tolist())
    default = [c for c in COUNTRY_CODES if c in available] or available

    selected = st.multiselect(
        "Countries to compare",
        options=available,
        default=default,
        format_func=lambda c: f"{c} — {COUNTRY_LABELS.get(c, c)}",
        help="Choose which markets appear in the chart and tables.",
    )

    if not selected:
        st.info("Select at least one country to see charts and tables.")
        render_disclaimer()
        return

    latest = latest_snapshot(df)
    latest_selected = latest[latest["country"].isin(selected)]

    render_best_market(latest_selected)
    st.divider()
    render_price_chart(df, selected)

    col_table, col_vol = st.columns([1.4, 1])
    with col_table:
        render_gap_table(latest, selected)
    with col_vol:
        render_volatility(df, selected)

    st.divider()
    render_disclaimer()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    st.caption(f"Dashboard generated {now} UTC · {len(df)} snapshot row(s) loaded")


if __name__ == "__main__":
    main()
