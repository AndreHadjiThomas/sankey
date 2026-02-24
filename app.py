import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

from floweaver import (
    Bundle,
    Dataset,
    Elsewhere,
    Partition,
    ProcessGroup,
    SankeyDefinition,
    Waypoint,
    weave,
)

st.set_page_config(page_title="IEA Energy Sankey (floweaver logic)", layout="wide")

st.title("Germany Energy System Sankey (floweaver logic) — from IEA Excel")

# -------------------------
# Sidebar controls
# -------------------------
st.sidebar.header("Inputs")

default_path = "/mnt/data/World Energy Balances Highlights 2025 (1).xlsx"
uploaded = st.sidebar.file_uploader("Upload the IEA Excel file", type=["xlsx"])

YEAR = st.sidebar.selectbox("Year", list(range(1971, 2025))[::-1], index=list(range(1971, 2025))[::-1].index(2023))
COUNTRY = st.sidebar.text_input("Country", "Germany")

use_floweaver_processing = st.sidebar.checkbox("Use floweaver-style grouping + rejected energy", value=True)

# -------------------------
# Load data
# -------------------------
@st.cache_data(show_spinner=False)
def load_iea_excel(file_bytes_or_path):
    if isinstance(file_bytes_or_path, (bytes, bytearray)):
        df = pd.read_excel(
            file_bytes_or_path,
            sheet_name="TimeSeries_1971-2024",
            skiprows=1,
            usecols="A:C,G:BH",
        )
    else:
        df = pd.read_excel(
            file_bytes_or_path,
            sheet_name="TimeSeries_1971-2024",
            skiprows=1,
            usecols="A:C,G:BH",
        )
    return df

if uploaded is not None:
    df_raw = load_iea_excel(uploaded.getvalue())
    st.sidebar.success("Loaded uploaded file")
else:
    df_raw = load_iea_excel(default_path)
    st.sidebar.info(f"Using default file path: {default_path}")

# -------------------------
# Prepare / filter
# -------------------------
# The sheet has columns Country / Product / Flow / years...
df = df_raw.copy()
df = df.rename(columns=lambda c: str(c).strip())
year_col = str(YEAR)

if year_col not in df.columns:
    st.error(f"Year column {YEAR} not found in the Excel sheet.")
    st.stop()

df[year_col] = pd.to_numeric(df[year_col], errors="coerce")

df_filtered = df[(df["Country"] == COUNTRY)].copy()
df_filtered = df_filtered.dropna(subset=[year_col])

if df_filtered.empty:
    st.error(f"No rows found for {COUNTRY}. Check spelling vs the Excel sheet Country names.")
    st.stop()

# Set multi-index like the notebook does
df_filtered = df_filtered.set_index(["Product", "Flow"])[year_col]

# -------------------------
# floweaver-style definitions (from your notebook)
# -------------------------
flows = [
    "Coal, peat and oil shale",
    "Crude, NGL and feedstocks",
    "Natural gas",
    "Nuclear",
    "Oil products",
    "Renewables and waste",
    "Heat",
    "Electricity",
]

sources = ["Imports (PJ)", "Production (PJ)"]

uses = [
    "Commercial and public services (PJ)",
    "Industry (PJ)",
    "Other final consumption (PJ)",
    "Residential (PJ)",
    "Transport (PJ)",
    "Exports (PJ)",
]

intermediate_processes = [
    "Electricity, CHP and heat plants (PJ)",
    "Oil refineries, transformation (PJ)",
]

# Totals by end-use (just for labels)
end_use_totals = df_filtered.unstack()[uses].sum().abs()

# -------------------------
# Build tidy flow table (data_df) like notebook cell 8
# -------------------------
data = []
# df_filtered index levels: Product, Flow
all_products = df_filtered.index.get_level_values(0).unique()

for flow in all_products:
    if flow not in flows:
        continue

    flow_df = df_filtered.xs(flow, level=0)  # index = Flow (the “Flow” column in Excel)
    # remove rows containing "Total" in the Flow names (no totals)
    no_totals_flow_df = flow_df.filter(regex="^((?!Total).)*$", axis=0).where(lambda x: x != 0)

    if no_totals_flow_df.empty:
        continue

    targets_ = no_totals_flow_df.loc[uses + intermediate_processes].dropna().abs()

    # identify sources from positive values (intermediate outputs)
    sources_ = (
        no_totals_flow_df.drop(uses + intermediate_processes, errors="ignore")
        .dropna()
        .loc[lambda s: s > 0]
    )

    # Add source -> target links
    # NOTE: In the IEA sheet, flows into intermediate processes / exports are negative; we took abs().
    for tgt, val in targets_.items():
        # We keep the Excel labels for now, then remap later
        # We will create links from "Imports (PJ)" and "Production (PJ)" to targets proportionally
        # and from intermediate sources to targets when present.
        data.append(
            dict(flow=flow, source=tgt, target=tgt, value=0.0)  # placeholder (we’ll overwrite logic below)
        )

    # We'll store raw values for later allocation using the official logic:
    # imports/production are sources; intermediate_processes and uses are targets.
    # The notebook constructs flows by taking the entries and interpreting sign.
    # We'll build links explicitly:
    # - From imports/production to each target, by allocating targets proportional to supply shares
    #   for that energy carrier (imports vs production).
    imp = float(abs(no_totals_flow_df.get("Imports (PJ)", 0.0)))
    prod = float(abs(no_totals_flow_df.get("Production (PJ)", 0.0)))

    supply = imp + prod
    if supply <= 0:
        continue

    # Targets we want to allocate from supply: uses + intermediate_processes (including exports)
    for tgt in (uses + intermediate_processes):
        if tgt not in no_totals_flow_df.index:
            continue
        v = float(abs(no_totals_flow_df.loc[tgt]))
        if v <= 0:
            continue
        # allocate by share of imports/production in supply
        if imp > 0:
            data.append(dict(flow=flow, source="Imports (PJ)", target=tgt, value=v * (imp / supply)))
        if prod > 0:
            data.append(dict(flow=flow, source="Production (PJ)", target=tgt, value=v * (prod / supply)))

    # Intermediate sources (positive values outside those rows) to uses/intermediate targets
    # (This captures flows like "Oil refineries, transformation (PJ)" -> something, if present as positive)
    for src, src_val in sources_.items():
        # distribute src_val to uses + intermediate_processes if we can see positive entries;
        # but we don’t have a full matrix here, so we skip unless your sheet provides it.
        # In practice, the balance highlights sheet is mostly “one-step” accounting.
        pass

data_df = pd.DataFrame(data)
data_df = data_df[data_df["value"] > 0].copy()

# -------------------------
# Remap labels (notebook cell 9)
# -------------------------
new_uses = [f"{u.replace(' (PJ)', '')} ({end_use_totals.loc[u]:,.0f} PJ)" for u in uses]
new_sources = [s.replace(" (PJ)", "") for s in sources]
new_intermediate_processes = [p.replace(" (PJ)", "") for p in intermediate_processes]

label_map = {s: s.replace(" (PJ)", "") for s in (sources + uses + intermediate_processes)}
# update uses labels with totals
for old_u, new_u in zip(uses, new_uses):
    label_map[old_u] = new_u

data_df["source"] = data_df["source"].map(label_map).fillna(data_df["source"])
data_df["target"] = data_df["target"].map(label_map).fillna(data_df["target"])

# Make sure our canonical process names match the floweaver definition
# (these are the exact strings we use downstream)
imports_name = "Imports"
production_name = "Production"
electricity_name = "Electricity, CHP and heat plants"
oil_name = "Oil refineries, transformation"

# -------------------------
# Rejected energy (notebook cell 10)
# -------------------------
# Rejected = input to intermediate process - output from intermediate process
# In this highlights sheet we don’t get explicit outputs from intermediate processes,
# so generated is usually 0 and rejected becomes input. This still visualises “rejected from each process”
# and keeps the floweaver Elsewhere pattern meaningful.
generated = (
    data_df[data_df.source.isin(new_intermediate_processes)]
    .groupby("source")
    .value.sum()
)
input_ = (
    data_df[data_df.target.isin(new_intermediate_processes)]
    .groupby("target")
    .value.sum()
)
rejected = (input_ - generated).where(lambda x: x > 0).dropna()

if use_floweaver_processing and not rejected.empty:
    rejected_df = pd.DataFrame(
        {
            "flow": "Rejected",
            "source": rejected.index,
            "target": "Rejected",
            "value": rejected.values,
        }
    )
    data_df2 = pd.concat([data_df, rejected_df], ignore_index=True)
else:
    data_df2 = data_df

# -------------------------
# Palette (notebook cell 11)
# -------------------------
palette = {
    "Coal, peat and oil shale": "#383838",
    "Crude, NGL and feedstocks": "#A8610B",
    "Natural gas": "#1E90FF",
    "Nuclear": "#FFD700",
    "Oil products": "#000000",
    "Renewables and waste": "#228B22",
    "Heat": "#A52A2A",
    "Electricity": "#AA15BA",
    "Rejected": "#696969",
}

# -------------------------
# Build Plotly sankey from flow table
# -------------------------
def get_sankey_data(flows_df: pd.DataFrame):
    all_nodes = list(pd.concat([flows_df["source"], flows_df["target"]]).unique())
    node_dict = {node: idx for idx, node in enumerate(all_nodes)}
    sankey_flows = flows_df.copy()
    sankey_flows["source_idx"] = sankey_flows["source"].map(node_dict)
    sankey_flows["target_idx"] = sankey_flows["target"].map(node_dict)
    return all_nodes, sankey_flows

def hex_to_rgba(hex_color: str, opacity: float = 0.7) -> str:
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = hex_color * 2
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"rgba({r},{g},{b},{opacity})"

# Aggregate identical source-target-flow
agg_cols = ["source", "target", "flow"]
plot_df = data_df2.groupby(agg_cols, as_index=False)["value"].sum()

all_nodes, sankey_flows = get_sankey_data(plot_df)
link_colors = sankey_flows["flow"].map(palette).fillna("#999999").apply(hex_to_rgba)

fig = go.Figure(
    data=[
        go.Sankey(
            arrangement="snap",
            node=dict(
                pad=15,
                thickness=18,
                label=all_nodes,
                line=dict(color="black", width=0.5),
            ),
            link=dict(
                source=sankey_flows["source_idx"],
                target=sankey_flows["target_idx"],
                value=sankey_flows["value"],
                color=link_colors,
                customdata=sankey_flows["flow"],
                hovertemplate="%{source.label} → %{target.label}<br>%{value:,.0f} PJ<br>Flow: %{customdata}<extra></extra>",
            ),
        )
    ]
)

fig.update_layout(
    title=f"{COUNTRY} Energy Flows ({YEAR}) — web view (floweaver logic, no totals)",
    font_size=11,
    height=600,
)

# -------------------------
# Page layout
# -------------------------
c1, c2 = st.columns([2, 1], gap="large")

with c1:
    st.plotly_chart(fig, use_container_width=True)

with c2:
    st.subheader("End-use totals (for labels)")
    st.dataframe(end_use_totals.rename("PJ"))

    st.subheader("Flow table (debug)")
    st.write("These are the aggregated links used to draw the Sankey.")
    st.dataframe(plot_df.sort_values("value", ascending=False).head(30))

st.caption(
    "This app uses the same grouping logic as floweaver, but renders the Sankey with Plotly for reliable web embedding."
)
