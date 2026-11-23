"""SONY Spreading Windows: when each recce plot can still be spread.

Reads Lenny's "Recce plots development timelines" Google Sheet (CSV export, no sign-in needed)
and shows which plots must be spread soon. Spreading is best before planting (fallow,
ploughed, furrowed), still possible while the cane is small, and too late after the
spreading deadline (planting + 2 months).

Run locally:  streamlit run spreading_app/app.py --server.port 8502
"""
import io
import re
from datetime import date, datetime, timedelta, timezone

import altair as alt
import folium
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium

PLOTS_URL = ("https://services7.arcgis.com/hmoHyeVfG7lIKwZ1/arcgis/rest/services/Project_10k/FeatureServer/1/query"
             "?where=1%3D1&outFields=field_designation,Area,project&outSR=4326&returnGeometry=true&f=geojson")
PLOT_RED = "#ff3b30"
SHEET_ID = "1_jL7BnnMzc_ZPOtNyJa3dVGmZ8ecR5-Lw_6ATVKNDL8"
SHEET_GID = "1532260065"
CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
SHEET_LINK = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
EAT = timezone(timedelta(hours=3))
DUE_SOON_DAYS = 14

HEADERS = {
    "PLOT NAME/ID": "plot", "AREA SAMPLED": "area_ha", "1ST PLOUGH": "first_plough",
    "2ND PLOUGH": "second_plough", "HARROW": "harrow", "FURROW": "furrow", "PLANTING": "planting",
    "SPREADING DEADLINE": "spreading_deadline", "MATURITY": "maturity", "RECCE": "recce",
    "CROP": "crop", "VARIETY": "variety",
}
PREP = [("first_plough", "1st plough"), ("second_plough", "2nd plough"), ("harrow", "Harrowed"), ("furrow", "Furrowed")]
STAGES = ["Not started", "1st plough", "2nd plough", "Harrowed", "Furrowed", "Planted", "Dropped"]
STATUSES = ["Window missed", "Due within 14 days", "Due later", "Not planted (ideal now)", "Dropped"]
STATUS_COLOURS = {
    "Window missed": "#c0392b", "Due within 14 days": "#e08e0b", "Due later": "#2f6fb0",
    "Not planted (ideal now)": "#2e8b57", "Dropped": "#9aa0a6",
}
PHASES = {"Ideal: not planted": "#2e8b57", "Still possible: cane small": "#e08e0b", "Too late": "#c0392b"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# ---------------------------------------------------------------- data
@st.cache_data(ttl=300, show_spinner="Reading Lenny's sheet…")
def load_sheet() -> tuple[pd.DataFrame, str]:
    r = requests.get(CSV_URL, timeout=30)
    r.raise_for_status()
    raw = pd.read_csv(io.StringIO(r.content.decode("utf-8")), dtype=str, header=None, keep_default_na=False)
    names = [HEADERS.get(re.sub(r"\s+", " ", str(h).strip().upper())) for h in raw.iloc[0]]
    if "plot" not in names:
        raise ValueError('The sheet has no "PLOT NAME/ID" column in row 1.')
    rows = []
    for i, values in enumerate(raw.iloc[1:].itertuples(index=False), start=2):
        rec = {"sheet_row": i, **{c: "" for c in HEADERS.values()}}
        for name, v in zip(names, values):
            if name:
                rec[name] = str(v).strip()
        if not rec["plot"] or rec["plot"].upper() == "TOTAL":
            continue
        rows.append(rec)
    df = pd.DataFrame(rows)
    df["area_ha"] = pd.to_numeric(df["area_ha"], errors="coerce")
    return df, datetime.now(EAT).strftime("%d %b %Y %H:%M")


@st.cache_data(ttl=3600, show_spinner="Loading plot boundaries from Project_10k…")
def load_plots() -> dict:
    """Plot boundaries from the Project_10k plots layer (layer 1), which can be read without signing in."""
    r = requests.get(PLOTS_URL, timeout=60)
    r.raise_for_status()
    gj = r.json()
    if "features" not in gj:
        raise ValueError(gj.get("error", {}).get("message", "unexpected answer from ArcGIS"))
    return gj


def bounds_of(geom: dict):
    pts = []
    def walk(c):
        if isinstance(c[0], (int, float)):
            pts.append(c)
        else:
            for x in c:
                walk(x)
    walk(geom["coordinates"])
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [[min(ys), min(xs)], [max(ys), max(xs)]]


def join_plots(gj: dict, s: pd.DataFrame) -> list[dict]:
    """Match each map shape to a sheet row by plot ID; a repeated ID (310A) is matched by the nearest area."""
    used, out = set(), []
    for f in gj["features"]:
        p = f["properties"]
        cands = s[(s["plot"] == p["field_designation"]) & ~s["sheet_row"].isin(used)]
        row = None
        if len(cands):
            row = cands.iloc[(cands["area_ha"].fillna(0) - (p["Area"] or 0)).abs().argsort().iloc[0]].to_dict()
            used.add(row["sheet_row"])
        out.append({"geometry": f["geometry"], "plot": p["field_designation"], "area_map": p["Area"] or 0,
                    "project": p["project"], "row": row, "bounds": bounds_of(f["geometry"])})
    return out


def parse(text: str):
    """'13-July-2026' -> (date, False); 'Aug-2026' -> (15 Aug 2026, True); anything else -> None."""
    s = str(text or "").strip().lower()
    m = re.fullmatch(r"(\d{1,2})[-\s]+([a-z]+)[-\s]+(\d{4})", s)
    if m and m.group(2)[:3] in MONTHS:
        return date(int(m.group(3)), MONTHS[m.group(2)[:3]], int(m.group(1))), False
    m = re.fullmatch(r"([a-z]+)[-\s]+(\d{4})", s)
    if m and m.group(1)[:3] in MONTHS:
        return date(int(m.group(2)), MONTHS[m.group(1)[:3]], 15), True
    return None


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue


def work_out_status(df: pd.DataFrame, today: date) -> pd.DataFrame:
    out = []
    for r in df.to_dict("records"):
        p = {k: parse(r[k]) for k in ("first_plough", "second_plough", "harrow", "furrow", "planting", "spreading_deadline")}
        dropped = bool(re.search(r"maize|dropped", f"{r['crop']} {r['spreading_deadline']}", re.I))
        stage = "Not started"
        for k, label in PREP:
            if p[k]:
                stage = label
        deadline = p["spreading_deadline"]
        if not deadline and p["planting"]:
            deadline = (add_months(p["planting"][0], 2), p["planting"][1])
        days = None
        if dropped:
            status, stage = "Dropped", "Dropped"
        elif p["planting"]:
            stage = "Planted"
            days = (deadline[0] - today).days
            status = "Window missed" if days < 0 else "Due within 14 days" if days <= DUE_SOON_DAYS else "Due later"
        else:
            status = "Not planted (ideal now)"
        prep_dates = [p[k][0] for k, _ in PREP if p[k]]
        out.append({
            **r, "status": status, "stage": stage,
            "prep_start": prep_dates[0] if prep_dates else None,
            "planting_date": p["planting"][0] if p["planting"] else None,
            "planting_month_only": bool(p["planting"] and p["planting"][1]),
            "deadline": None if dropped or not deadline else deadline[0],
            "days_left": None if dropped else days,
            "month_only": ", ".join(k.replace("_", " ") for k, v in p.items() if v and v[1]),
        })
    s = pd.DataFrame(out)
    group = s["status"].map({"Window missed": 0, "Due within 14 days": 1, "Due later": 1, "Not planted (ideal now)": 2, "Dropped": 3})
    s["_g"] = group
    s["_stage"] = -s["stage"].map(STAGES.index)
    s["_dl"] = pd.to_datetime(s["deadline"])
    s = s.sort_values(["_g", "_dl", "_stage", "plot"], na_position="last").drop(columns=["_g", "_stage", "_dl"]).reset_index(drop=True)
    # a readable, unique label per sheet row (310A has two rows)
    dup = s["plot"].duplicated(keep=False)
    s["label"] = s["plot"].where(~dup, s["plot"] + " (" + s["area_ha"].map(lambda a: f"{a:g}") + " ha)")
    return s


def fmt_date(d) -> str:
    if d is None or (isinstance(d, float) and pd.isna(d)) or pd.isna(d):
        return "—"
    d = pd.Timestamp(d)
    return f"{d.day} {d.strftime('%b %Y')}"


# ---------------------------------------------------------------- page
st.set_page_config(page_title="SONY Spreading Windows", page_icon="🌱", layout="wide")

with st.sidebar:
    st.markdown("**Data:** Lenny's [Recce plots development timelines]({}) sheet.".format(SHEET_LINK))
    if st.button("Reload the sheet now"):
        load_sheet.clear()
    st.caption("The sheet is re-read automatically every 5 minutes.")

try:
    sheet, loaded_at = load_sheet()
except Exception as e:  # network or sheet layout problem
    st.error(f"Couldn't read Lenny's sheet: {e}")
    st.stop()

today = datetime.now(EAT).date()
s = work_out_status(sheet, today)
summary = (s.groupby("status").agg(lines=("plot", "size"), area_ha=("area_ha", "sum"))
           .reindex(STATUSES).fillna({"lines": 0, "area_ha": 0}))
upcoming = s[s["days_left"].notna() & (s["days_left"] >= 0)].sort_values("days_left")

st.title("SONY spreading windows")
soon = summary.loc["Due within 14 days"]
lede = f"**{soon.area_ha:.1f} ha** must be spread in the next {DUE_SOON_DAYS} days, while the cane is still small ({int(soon.lines)} sheet rows)."
if len(upcoming):
    first = upcoming.iloc[0]["deadline"]
    names = ", ".join(dict.fromkeys(upcoming[upcoming["deadline"] == first]["plot"]))
    lede += f" Next deadline: **{names}** on **{fmt_date(first)}**."
st.markdown(lede)
st.caption("Spreading is best before planting, while a plot is fallow, ploughed or furrowed. After planting it's still possible "
           "until the spreading deadline (planting + 2 months), while the cane is small. The sheet has no spread date yet, "
           f"so this page can't show what's already been spread. Today: {fmt_date(today)} · sheet read {loaded_at} EAT.")

cols = st.columns(len(STATUSES))
subs = {"Window missed": "cane likely too tall", "Due within 14 days": "planted, cane small", "Due later": "planted, cane small",
        "Not planted (ideal now)": "fallow or prepared", "Dropped": "not sugarcane"}
for col, name in zip(cols, STATUSES):
    row = summary.loc[name]
    col.markdown(f"<span style='color:{STATUS_COLOURS[name]}'>■</span> {name}", unsafe_allow_html=True)
    col.metric(name, int(row.lines), label_visibility="collapsed")
    col.caption(f"{row.area_ha:.2f} ha · {subs[name]}")

# ---- map
st.subheader("Map")
try:
    feats = join_plots(load_plots(), s)
except Exception as e:
    feats = []
    st.warning(f"Couldn't load the plot boundaries from Project_10k: {e}")
MAP_ZOOM = 15  # about a 500 m scale bar at this latitude
if feats:
    feats = [f for f in feats if f["row"]]  # only plots that are in Lenny's sheet
    by_label = {f["row"]["label"]: f for f in feats}
    status_of = dict(zip(s["label"], s["status"]))
    choices = [l for l in s["label"] if l in by_label]  # most urgent first
    nxt = upcoming.iloc[0]["label"] if len(upcoming) else choices[0]
    c1, c2 = st.columns([2, 3])
    pick = c1.selectbox("Centre the map on", choices, index=choices.index(nxt) if nxt in choices else 0,
                        format_func=lambda l: f"{l} · {status_of.get(l, '')}")
    c2.markdown(
        "<div style='padding-top:2.1rem;font-size:0.85rem'>"
        + " &nbsp; ".join(f"<span style='color:{STATUS_COLOURS[k]}'>●</span> {k}" for k in STATUSES)
        + f" &nbsp; <span style='color:{PLOT_RED}'>▢</span> plot outline</div>",
        unsafe_allow_html=True)

    (b0, b1) = by_label[pick]["bounds"]
    m = folium.Map(location=[(b0[0] + b1[0]) / 2, (b0[1] + b1[1]) / 2], zoom_start=MAP_ZOOM,
                   tiles=None, control_scale=True, max_zoom=19)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Imagery © Esri, Maxar, Earthstar Geographics", name="Satellite", max_zoom=19, max_native_zoom=18, show=True).add_to(m)
    folium.TileLayer("OpenStreetMap", name="Street map", show=False).add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
        attr="Labels © Esri", name="Place names", overlay=True, max_zoom=19, max_native_zoom=18).add_to(m)

    for f in feats:
        r = f["row"]
        is_sel = r["label"] == pick
        tip = (f"<b>{r['label']}</b><br>{r['status']}<br>Stage: {r['stage']}<br>Area: {f['area_map']:.2f} ha (map)"
               + (f"<br>Spread by: {fmt_date(r['deadline'])}" if r["deadline"] else "")
               + (f" ({int(r['days_left'])} days)" if r["days_left"] is not None and not pd.isna(r["days_left"]) else ""))
        folium.GeoJson(
            {"type": "Feature", "geometry": f["geometry"], "properties": {}},
            style_function=lambda _, sel=is_sel: {
                "color": PLOT_RED, "weight": 3.5 if sel else 1.6, "opacity": 1 if sel else 0.75, "fill": True, "fillOpacity": 0.0},
            tooltip=folium.Tooltip(tip, sticky=True),
        ).add_to(m)
        (p0, p1) = f["bounds"]
        folium.CircleMarker(
            location=[(p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2], radius=6, weight=1.5, color="#111",
            fill=True, fill_color=STATUS_COLOURS[r["status"]], fill_opacity=0.95,
            tooltip=folium.Tooltip(tip, sticky=True)).add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    st_folium(m, height=560, use_container_width=True, returned_objects=[], key=f"map-{pick}")
    st.caption("Opens on the plot with the next spreading deadline. Pick another plot above to move there, or zoom out to see the rest. "
               "Hover a plot or dot for its dates. The layer button (top right) switches to a street map or hides place names.")

# ---- hectares due per deadline week
st.subheader("Hectares to spread, by deadline week")
st.caption("Planted plots only, grouped by the week (Monday to Sunday) their deadline falls in.")
if len(upcoming):
    monday = lambda d: d - timedelta(days=d.weekday())
    weeks = []
    w = monday(today)
    last = monday(upcoming["deadline"].max())
    while w <= last:
        rs = upcoming[(upcoming["deadline"] >= w) & (upcoming["deadline"] <= w + timedelta(days=6))]
        weeks.append({"week": pd.Timestamp(w), "week_label": f"{w.day} {w.strftime('%b')}", "area_ha": round(rs["area_ha"].sum(), 2),
                      "plots": ", ".join(rs["plot"]), "urgent": (w - today).days <= DUE_SOON_DAYS})
        w += timedelta(days=7)
    wk = pd.DataFrame(weeks)
    base = alt.Chart(wk).encode(x=alt.X("week_label:N", sort=None, title="Week starting (Monday)", axis=alt.Axis(labelAngle=0)))
    bars = base.mark_bar(cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
        y=alt.Y("area_ha:Q", title="Hectares due"),
        color=alt.condition("datum.urgent", alt.value(STATUS_COLOURS["Due within 14 days"]), alt.value(STATUS_COLOURS["Due later"])),
        tooltip=[alt.Tooltip("week_label:N", title="Week of"), alt.Tooltip("area_ha:Q", title="Hectares"), alt.Tooltip("plots:N", title="Plots")])
    labels = base.mark_text(dy=-6).encode(y="area_ha:Q", text=alt.Text("area_ha:Q", format=".1f"))
    st.altair_chart((bars + labels).properties(height=260), width="stretch")
else:
    st.info("No planted plot has a deadline still ahead.")

# ---- spread next
st.subheader("Spread next")
st.caption("Every line in the sheet, most urgent first. A month on its own (e.g. \"Oct-2026\") is read as the 15th.")
table = pd.DataFrame({
    "Plot": s["plot"], "Area (ha)": s["area_ha"], "Stage now": s["stage"],
    "Planted": [fmt_date(d) + (" (month)" if m else "") for d, m in zip(s["planting_date"], s["planting_month_only"])],
    "Spread by": s["deadline"].map(fmt_date), "Days left": s["days_left"].astype("Int64"), "Status": s["status"],
})
styled = table.style.map(lambda v: f"color: {STATUS_COLOURS.get(v, 'inherit')}; font-weight: 600", subset=["Status"]) \
    .format({"Area (ha)": "{:.2f}"}, na_rep="—")
st.dataframe(styled, hide_index=True, width="stretch", height=min(38 * (len(table) + 1), 640))

# ---- Gantt
st.subheader("Spreading windows by plot")
st.caption("Green: ploughed or furrowed but not planted (ideal). Amber: planted, cane small, until the deadline. "
           "Red: past the deadline. ◆ planting date. Dashed line: today.")
segs = []
for r in s.to_dict("records"):
    if r["status"] == "Dropped":
        continue
    start, plant, dl = r["prep_start"], r["planting_date"], r["deadline"]
    if plant:
        if start and plant > start:
            segs.append((r["label"], start, plant, "Ideal: not planted"))
        if dl and dl > plant:
            segs.append((r["label"], plant, dl, "Still possible: cane small"))
        if dl and dl < today:
            segs.append((r["label"], dl, today, "Too late"))
    elif start and today > start:
        segs.append((r["label"], start, today, "Ideal: not planted"))
g = pd.DataFrame(segs, columns=["label", "start", "end", "phase"])
order = [l for l, st_ in zip(s["label"], s["status"]) if st_ != "Dropped"]
if len(g):
    g["start"], g["end"] = pd.to_datetime(g["start"]), pd.to_datetime(g["end"])
    y = alt.Y("label:N", sort=order, title=None, axis=alt.Axis(labelLimit=160))
    gantt = alt.Chart(g).mark_bar(height=11, opacity=0.8).encode(
        x=alt.X("start:T", title=None, axis=alt.Axis(format="%b %Y", tickCount="month")), x2="end:T", y=y,
        color=alt.Color("phase:N", scale=alt.Scale(domain=list(PHASES), range=list(PHASES.values())), legend=alt.Legend(orient="top", title=None)),
        tooltip=[alt.Tooltip("label:N", title="Plot"), alt.Tooltip("phase:N", title="Phase"),
                 alt.Tooltip("start:T", title="From", format="%d %b %Y"), alt.Tooltip("end:T", title="To", format="%d %b %Y")])
    pts = s[s["planting_date"].notna() & (s["status"] != "Dropped")][["label", "planting_date", "planting_month_only"]].copy()
    pts["planting_date"] = pd.to_datetime(pts["planting_date"])
    diamonds = alt.Chart(pts).mark_point(shape="diamond", size=70, filled=True, color="#222").encode(
        x="planting_date:T", y=y, tooltip=[alt.Tooltip("label:N", title="Plot"), alt.Tooltip("planting_date:T", title="Planted", format="%d %b %Y")])
    rule = alt.Chart(pd.DataFrame({"today": [pd.Timestamp(today)]})).mark_rule(strokeDash=[4, 3], color="#444").encode(x="today:T")
    st.altair_chart((gantt + diamonds + rule).properties(height=max(300, 20 * len(order))), width="stretch")

with st.expander("How this page works it out"):
    st.markdown(f"""
- All dates come from Lenny's sheet, re-read every 5 minutes (or with **Reload the sheet now**).
- The spreading deadline is the sheet's own column; where it's blank but a planting date exists, it's planting + 2 months.
- **Window missed:** the deadline has passed, so the cane is probably too tall. **Due within {DUE_SOON_DAYS} days:** the deadline is today or in the next {DUE_SOON_DAYS} days.
- **Not planted:** no planting date yet. *Stage now* is the latest of 1st plough, 2nd plough, harrow and furrow with a date; "Not done" counts as skipped.
- Rows whose crop is maize, or whose deadline cell says "dropped", count as **Dropped**.
- Areas are the sheet's "area sampled" figures. 310A has two rows (2.15 and 0.93 ha).
""")
