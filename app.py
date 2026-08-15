"""
AstroLotto Score v3 – Experimentelles Scoring-System
=====================================================
- skyfield (JPL DE421) für Planetenpositionen
- 80+ Städte mit Zeitzonen
- AstroWeather für Di & Sa + 14-Tage-Verlauf
- Gewichtete Begründungen, Mond/Merkur-Badges
- Profil-Historie, Export, Di-vs-Sa-Vergleich
- Grobe Häuser-Näherung (ganz Zeichen)

NUR ZUR UNTERHALTUNG – keine Gewinngarantie.
"""

import streamlit as st
from datetime import date, datetime, timedelta, timezone
import math
import hashlib

from skyfield.api import load

ts = load.timescale()
eph = load("de421.bsp")

planets = {
    "sun": eph["sun"],
    "moon": eph["moon"],
    "mercury": eph["mercury"],
    "venus": eph["venus"],
    "earth": eph["earth"],
    "mars": eph["mars"],
    "jupiter": eph["jupiter barycenter"],
    "saturn": eph["saturn barycenter"],
    "uranus": eph["uranus barycenter"],
    "neptune": eph["neptune barycenter"],
    "pluto": eph["pluto barycenter"],
}

st.set_page_config(
    page_title="AstroLotto Score",
    page_icon="🍀",
    layout="centered",
    initial_sidebar_state="expanded",
)

CITY_DATA = {
    "Berlin": (52.5200, 13.4050, "Europe/Berlin"),
    "Hamburg": (53.5511, 9.9937, "Europe/Berlin"),
    "München": (48.1351, 11.5820, "Europe/Berlin"),
    "Köln": (50.9375, 6.9603, "Europe/Berlin"),
    "Frankfurt am Main": (50.1109, 8.6821, "Europe/Berlin"),
    "Stuttgart": (48.7758, 9.1829, "Europe/Berlin"),
    "Düsseldorf": (51.2277, 6.7735, "Europe/Berlin"),
    "Leipzig": (51.3397, 12.3731, "Europe/Berlin"),
    "Dortmund": (51.5136, 7.4653, "Europe/Berlin"),
    "Essen": (51.4556, 7.0116, "Europe/Berlin"),
    "Bremen": (53.0793, 8.8017, "Europe/Berlin"),
    "Dresden": (51.0504, 13.7373, "Europe/Berlin"),
    "Hannover": (52.3759, 9.7320, "Europe/Berlin"),
    "Nürnberg": (49.4521, 11.0767, "Europe/Berlin"),
    "Duisburg": (51.4344, 6.7623, "Europe/Berlin"),
    "Bochum": (51.4818, 7.2162, "Europe/Berlin"),
    "Wuppertal": (51.2562, 7.1508, "Europe/Berlin"),
    "Bielefeld": (52.0302, 8.5325, "Europe/Berlin"),
    "Bonn": (50.7374, 7.0982, "Europe/Berlin"),
    "Münster": (51.9607, 7.6261, "Europe/Berlin"),
    "Karlsruhe": (49.0069, 8.4037, "Europe/Berlin"),
    "Mannheim": (49.4875, 8.4660, "Europe/Berlin"),
    "Augsburg": (48.3705, 10.8978, "Europe/Berlin"),
    "Wiesbaden": (50.0782, 8.2398, "Europe/Berlin"),
    "Mönchengladbach": (51.1805, 6.4428, "Europe/Berlin"),
    "Gelsenkirchen": (51.5177, 7.0857, "Europe/Berlin"),
    "Braunschweig": (52.2689, 10.5268, "Europe/Berlin"),
    "Kiel": (54.3233, 10.1228, "Europe/Berlin"),
    "Aachen": (50.7753, 6.0839, "Europe/Berlin"),
    "Magdeburg": (52.1205, 11.6276, "Europe/Berlin"),
    "Freiburg": (47.9990, 7.8421, "Europe/Berlin"),
    "Krefeld": (51.3388, 6.5853, "Europe/Berlin"),
    "Lübeck": (53.8655, 10.6866, "Europe/Berlin"),
    "Oberhausen": (51.4963, 6.8515, "Europe/Berlin"),
    "Erfurt": (50.9848, 11.0299, "Europe/Berlin"),
    "Rostock": (54.0924, 12.0991, "Europe/Berlin"),
    "Mainz": (49.9929, 8.2473, "Europe/Berlin"),
    "Kassel": (51.3127, 9.4797, "Europe/Berlin"),
    "Hagen": (51.3671, 7.4633, "Europe/Berlin"),
    "Hamm": (51.6739, 7.8159, "Europe/Berlin"),
    "Saarbrücken": (49.2402, 6.9969, "Europe/Berlin"),
    "Potsdam": (52.3906, 13.0645, "Europe/Berlin"),
    "Wien": (48.2082, 16.3738, "Europe/Vienna"),
    "Graz": (47.0707, 15.4395, "Europe/Vienna"),
    "Linz": (48.3069, 14.2858, "Europe/Vienna"),
    "Salzburg": (47.8095, 13.0550, "Europe/Vienna"),
    "Innsbruck": (47.2692, 11.4041, "Europe/Vienna"),
    "Zürich": (47.3769, 8.5417, "Europe/Zurich"),
    "Genf": (46.2044, 6.1432, "Europe/Zurich"),
    "Basel": (47.5596, 7.5886, "Europe/Zurich"),
    "Bern": (46.9480, 7.4474, "Europe/Zurich"),
    "Lausanne": (46.5197, 6.6323, "Europe/Zurich"),
    "Luxemburg": (49.6116, 6.1319, "Europe/Luxembourg"),
    "Amsterdam": (52.3676, 4.9041, "Europe/Amsterdam"),
    "Brüssel": (50.8503, 4.3517, "Europe/Brussels"),
    "Paris": (48.8566, 2.3522, "Europe/Paris"),
    "London": (51.5074, -0.1278, "Europe/London"),
    "Madrid": (40.4168, -3.7038, "Europe/Madrid"),
    "Barcelona": (41.3874, 2.1686, "Europe/Madrid"),
    "Rom": (41.9028, 12.4964, "Europe/Rome"),
    "Mailand": (45.4642, 9.1900, "Europe/Rome"),
    "Lissabon": (38.7223, -9.1393, "Europe/Lisbon"),
    "Prag": (50.0755, 14.4378, "Europe/Prague"),
    "Warschau": (52.2297, 21.0122, "Europe/Warsaw"),
    "Budapest": (47.4979, 19.0402, "Europe/Budapest"),
    "Kopenhagen": (55.6761, 12.5683, "Europe/Copenhagen"),
    "Stockholm": (59.3293, 18.0686, "Europe/Stockholm"),
    "Oslo": (59.9139, 10.7522, "Europe/Oslo"),
    "Helsinki": (60.1699, 24.9384, "Europe/Helsinki"),
    "Dublin": (53.3498, -6.2603, "Europe/Dublin"),
    "Athen": (37.9838, 23.7275, "Europe/Athens"),
    "Istanbul": (41.0082, 28.9784, "Europe/Istanbul"),
    "New York": (40.7128, -74.0060, "America/New_York"),
    "Los Angeles": (34.0522, -118.2437, "America/Los_Angeles"),
    "Toronto": (43.6532, -79.3832, "America/Toronto"),
    "Dubai": (25.2048, 55.2708, "Asia/Dubai"),
    "Bangkok": (13.7563, 100.5018, "Asia/Bangkok"),
    "Singapur": (1.3521, 103.8198, "Asia/Singapore"),
    "Sydney": (-33.8688, 151.2093, "Australia/Sydney"),
    "Melbourne": (-37.8136, 144.9631, "Australia/Melbourne"),
    "Kapstadt": (-33.9249, 18.4241, "Africa/Johannesburg"),
    "Andere / Unbekannt": (50.0, 10.0, "UTC"),
}


def ecliptic_longitude(body_name: str, dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    t = ts.from_datetime(dt)
    astrometric = planets["earth"].at(t).observe(planets[body_name])
    _, lon, _ = astrometric.ecliptic_latlon()
    return lon.degrees % 360.0


def norm_deg(deg: float) -> float:
    return deg % 360.0


def angle_diff(a: float, b: float) -> float:
    d = abs(norm_deg(a - b))
    return min(d, 360.0 - d)


def is_aspect(lon1: float, lon2: float, aspect_angle: float, orb: float = 8.0) -> bool:
    return abs(angle_diff(lon1, lon2) - aspect_angle) <= orb


def moon_phase_fraction(dt: datetime) -> float:
    sun = ecliptic_longitude("sun", dt)
    moon = ecliptic_longitude("moon", dt)
    return norm_deg(moon - sun) / 360.0


def moon_phase_label(frac: float) -> str:
    if frac < 0.03 or frac > 0.97:
        return "Neumond"
    if 0.22 < frac < 0.28:
        return "Zunehmende Sichel / Halbmond"
    if 0.47 < frac < 0.53:
        return "Vollmond"
    if 0.72 < frac < 0.78:
        return "Abnehmender Halbmond"
    if frac < 0.5:
        return "Zunehmender Mond"
    return "Abnehmender Mond"


def mercury_retrograde(dt: datetime) -> bool:
    lon0 = ecliptic_longitude("mercury", dt)
    lon1 = ecliptic_longitude("mercury", dt + timedelta(days=1))
    delta = (lon1 - lon0 + 180) % 360 - 180
    return delta < 0


def part_of_fortune(asc: float, sun: float, moon: float, is_day: bool) -> float:
    if is_day:
        return norm_deg(asc + moon - sun)
    return norm_deg(asc + sun - moon)


def approx_ascendant(dt: datetime, lat: float, lon: float) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    t = ts.from_datetime(dt)
    gast = t.gast * 15.0
    lst = norm_deg(gast + lon)
    eps = 23.439
    asc = math.degrees(
        math.atan2(
            math.cos(math.radians(lst)),
            -(
                math.sin(math.radians(lst)) * math.cos(math.radians(eps))
                + math.tan(math.radians(lat)) * math.sin(math.radians(eps))
            ),
        )
    )
    return norm_deg(asc)


def sign_index(lon: float) -> int:
    return int(lon // 30) % 12


def whole_sign_house(planet_lon: float, asc_lon: float) -> int:
    return ((sign_index(planet_lon) - sign_index(asc_lon)) % 12) + 1


def score_general(dt: datetime):
    reasons = []
    points = 50.0

    sun = ecliptic_longitude("sun", dt)
    moon = ecliptic_longitude("moon", dt)
    venus = ecliptic_longitude("venus", dt)
    jupiter = ecliptic_longitude("jupiter", dt)
    saturn = ecliptic_longitude("saturn", dt)
    uranus = ecliptic_longitude("uranus", dt)
    mercury = ecliptic_longitude("mercury", dt)

    def add(delta, text):
        nonlocal points
        points += delta
        reasons.append((text, delta))

    if is_aspect(jupiter, sun, 120, 8) or is_aspect(jupiter, sun, 60, 6):
        add(12, "Jupiter harmonisch zur Sonne")
    if is_aspect(jupiter, moon, 120, 8) or is_aspect(jupiter, moon, 60, 6):
        add(10, "Jupiter harmonisch zum Mond")
    if is_aspect(jupiter, venus, 120, 8) or is_aspect(jupiter, venus, 0, 6):
        add(11, "Jupiter–Venus Glücksaspekt")
    if is_aspect(jupiter, sun, 90, 7) or is_aspect(jupiter, sun, 180, 7):
        add(-8, "Jupiter hart zur Sonne")

    if is_aspect(uranus, mercury, 0, 6) or is_aspect(uranus, mercury, 120, 7):
        add(14, "Uranus aktiviert Merkur (Zahlen/Tickets)")
    if is_aspect(uranus, jupiter, 0, 6) or is_aspect(uranus, jupiter, 120, 7):
        add(13, "Uranus–Jupiter (plötzliches Glück)")
    if is_aspect(uranus, venus, 0, 6):
        add(9, "Uranus–Venus (unerwarteter Geldfluss)")

    phase = moon_phase_fraction(dt)
    if 0.1 < phase < 0.45:
        add(7, "Zunehmender Mond")
    elif 0.55 < phase < 0.9:
        add(-4, "Abnehmender Mond")
    if abs(phase - 0.5) < 0.04:
        add(5, "Nahe Vollmond")

    if mercury_retrograde(dt):
        add(-7, "Merkur rückläufig")

    if is_aspect(saturn, jupiter, 90, 6) or is_aspect(saturn, jupiter, 180, 6):
        add(-9, "Saturn belastet Jupiter")
    if is_aspect(saturn, venus, 90, 6):
        add(-6, "Saturn–Venus Spannung")

    if is_aspect(venus, jupiter, 120, 7) or is_aspect(venus, jupiter, 60, 5):
        add(8, "Venus–Jupiter Trigon/Sextil")

    asc = approx_ascendant(dt, 50.0, 10.0)
    for lon, name in [(jupiter, "Jupiter"), (uranus, "Uranus"), (venus, "Venus")]:
        h = whole_sign_house(lon, asc)
        if h in (5, 8, 11):
            add(4, f"{name} im {h}. Haus (Spekulation/plötzlich/Gewinne)")

    points = max(0.0, min(100.0, points))
    return points, reasons


def score_personal(birth_dt, query_dt, lat, lon):
    reasons = []
    points = 48.0

    sun_n = ecliptic_longitude("sun", birth_dt)
    moon_n = ecliptic_longitude("moon", birth_dt)
    mercury_n = ecliptic_longitude("mercury", birth_dt)
    venus_n = ecliptic_longitude("venus", birth_dt)
    jupiter_n = ecliptic_longitude("jupiter", birth_dt)

    asc_n = approx_ascendant(birth_dt, lat, lon)
    is_day = 6 <= birth_dt.hour < 18
    pof = part_of_fortune(asc_n, sun_n, moon_n, is_day)

    jupiter_t = ecliptic_longitude("jupiter", query_dt)
    uranus_t = ecliptic_longitude("uranus", query_dt)
    venus_t = ecliptic_longitude("venus", query_dt)
    saturn_t = ecliptic_longitude("saturn", query_dt)
    moon_t = ecliptic_longitude("moon", query_dt)

    def add(delta, text):
        nonlocal points
        points += delta
        reasons.append((text, delta))

    if is_aspect(jupiter_t, sun_n, 0, 6) or is_aspect(jupiter_t, sun_n, 120, 7):
        add(14, "Jupiter-Transit zur radix Sonne")
    if is_aspect(jupiter_t, moon_n, 0, 6) or is_aspect(jupiter_t, moon_n, 120, 7):
        add(12, "Jupiter-Transit zum radix Mond")
    if is_aspect(jupiter_t, venus_n, 0, 6) or is_aspect(jupiter_t, venus_n, 120, 7):
        add(11, "Jupiter-Transit zur radix Venus")
    if is_aspect(jupiter_t, jupiter_n, 0, 5):
        add(15, "Jupiter-Return / Rückkehr-Nähe")

    if is_aspect(jupiter_t, pof, 0, 6) or is_aspect(jupiter_t, pof, 120, 7):
        add(13, "Jupiter aktiviert Part of Fortune")
    if is_aspect(uranus_t, pof, 0, 5) or is_aspect(uranus_t, pof, 120, 6):
        add(12, "Uranus aktiviert Part of Fortune")
    if is_aspect(moon_t, pof, 0, 5):
        add(7, "Mond über Part of Fortune")

    if is_aspect(uranus_t, mercury_n, 0, 5) or is_aspect(uranus_t, mercury_n, 120, 6):
        add(13, "Uranus auf radix Merkur (Tickets/Zahlen)")
    if is_aspect(uranus_t, jupiter_n, 0, 5) or is_aspect(uranus_t, jupiter_n, 120, 6):
        add(12, "Uranus auf radix Jupiter")
    if is_aspect(uranus_t, venus_n, 0, 5):
        add(9, "Uranus auf radix Venus")

    if is_aspect(saturn_t, jupiter_n, 90, 6) or is_aspect(saturn_t, jupiter_n, 180, 6):
        add(-10, "Saturn belastet radix Jupiter")
    if is_aspect(saturn_t, pof, 90, 5) or is_aspect(saturn_t, pof, 180, 5):
        add(-8, "Saturn belastet Part of Fortune")

    if is_aspect(venus_t, jupiter_n, 120, 6) or is_aspect(venus_t, jupiter_n, 0, 5):
        add(8, "Venus-Transit zu radix Jupiter")
    if is_aspect(moon_t, jupiter_n, 0, 5) or is_aspect(moon_t, jupiter_n, 120, 6):
        add(6, "Mond–Jupiter persönlich")

    for tlon, name in [(jupiter_t, "Jupiter"), (uranus_t, "Uranus")]:
        h = whole_sign_house(tlon, asc_n)
        if h in (5, 8, 11):
            add(5, f"Transit-{name} im radix {h}. Haus")

    points = max(0.0, min(100.0, points))
    return points, reasons


def luck_symbol(score: float) -> str:
    if score >= 80:
        return "🍀🍀🍀"
    if score >= 65:
        return "🍀🍀"
    if score >= 50:
        return "🍀"
    if score >= 35:
        return "🌱"
    return "🌑"


def score_color(score: float) -> str:
    if score >= 70:
        return "#2e7d32"
    if score >= 50:
        return "#f9a825"
    return "#c62828"


def next_weekday(d: date, weekday: int) -> date:
    days_ahead = (weekday - d.weekday()) % 7
    return d + timedelta(days=days_ahead)


def mock_jackpot_info(d: date) -> dict:
    seed = int(d.strftime("%Y%m%d"))
    h = hashlib.md5(str(seed).encode()).hexdigest()
    base = int(h[:6], 16) % 40 + 5
    tue = next_weekday(d, 1)
    sat = next_weekday(d, 5)
    return {
        "tuesday": {
            "date": tue,
            "jackpot_mio": round(base + (int(h[6:8], 16) % 10), 1),
            "tips_mio": round(18 + (int(h[8:10], 16) % 15), 1),
        },
        "saturday": {
            "date": sat,
            "jackpot_mio": round(base + 8 + (int(h[10:12], 16) % 12), 1),
            "tips_mio": round(28 + (int(h[12:14], 16) % 20), 1),
        },
    }


def format_reasons(reasons) -> str:
    lines = []
    for text, delta in sorted(reasons, key=lambda x: -abs(x[1])):
        sign = f"+{delta:.0f}" if delta > 0 else f"{delta:.0f}"
        lines.append(f"• {text} ({sign})")
    return "\n".join(lines) if lines else "• Keine starken Faktoren"


if "profiles" not in st.session_state:
    st.session_state.profiles = []

st.title("🍀 AstroLotto Score")
st.caption("v3 · skyfield · 14-Tage-Verlauf · AstroWeather Di/Sa · gewichtete Faktoren")

with st.expander("⚠️ Wichtiger Hinweis", expanded=False):
    st.markdown(
        """
        **Nur zur Unterhaltung.** Astrologie ist keine wissenschaftlich belegte Methode,
        Lottoziehungen vorherzusagen. Die Gewinnwahrscheinlichkeit bleibt extrem niedrig.
        Ephemeriden: skyfield + JPL DE421. Jackpot/Tipps = Platzhalter.
        """
    )

st.markdown("---")
st.subheader("Persönliche Daten")

if st.session_state.profiles:
    labels = [
        f"{p['birth_date']} {p['birth_time']} · {p['city']}" for p in st.session_state.profiles
    ]
    choice = st.selectbox("Gespeichertes Profil laden (optional)", ["— Neu eingeben —"] + labels)
    if choice != "— Neu eingeben —":
        idx = labels.index(choice)
        p = st.session_state.profiles[idx]
        default_bdate = date.fromisoformat(p["birth_date"])
        default_btime = datetime.strptime(p["birth_time"], "%H:%M").time()
        default_city = p["city"]
    else:
        default_bdate = date(1990, 5, 15)
        default_btime = datetime.strptime("12:00", "%H:%M").time()
        default_city = "Berlin"
else:
    default_bdate = date(1990, 5, 15)
    default_btime = datetime.strptime("12:00", "%H:%M").time()
    default_city = "Berlin"

col1, col2, col3 = st.columns(3)
with col1:
    birth_date = st.date_input("Geburtsdatum", value=default_bdate)
with col2:
    birth_time = st.time_input("Geburtsuhrzeit", value=default_btime)
with col3:
    query_date = st.date_input("Abfrage-Datum", value=date.today())

city_names = sorted(CITY_DATA.keys())
try:
    default_idx = city_names.index(default_city)
except ValueError:
    default_idx = city_names.index("Berlin")
city_choice = st.selectbox("Geburtsort", city_names, index=default_idx)
lat, lon, _tz = CITY_DATA[city_choice]
st.caption(f"{lat:.4f}°, {lon:.4f}° · TZ: {_tz} · {len(CITY_DATA)} Städte")

save_profile = st.checkbox("Profil für diese Sitzung speichern", value=True)

st.markdown("---")

if st.button("Score berechnen", type="primary", use_container_width=True):
    with st.spinner("Berechne Ephemeriden & Scores …"):
        birth_dt = datetime.combine(birth_date, birth_time).replace(tzinfo=timezone.utc)
        query_dt = datetime.combine(query_date, datetime.strptime("12:00", "%H:%M").time()).replace(
            tzinfo=timezone.utc
        )

        if save_profile:
            entry = {
                "birth_date": birth_date.isoformat(),
                "birth_time": birth_time.strftime("%H:%M"),
                "city": city_choice,
            }
            st.session_state.profiles = [p for p in st.session_state.profiles if p != entry]
            st.session_state.profiles.insert(0, entry)
            st.session_state.profiles = st.session_state.profiles[:8]

        gen_score, gen_reasons = score_general(query_dt)
        per_score, per_reasons = score_personal(birth_dt, query_dt, lat, lon)
        comb_score = (gen_score + per_score) / 2.0

        phase_frac = moon_phase_fraction(query_dt)
        phase_label = moon_phase_label(phase_frac)
        merc_rx = mercury_retrograde(query_dt)

        jack = mock_jackpot_info(query_date)

        tue_dt = datetime.combine(jack["tuesday"]["date"], datetime.strptime("18:00", "%H:%M").time()).replace(
            tzinfo=timezone.utc
        )
        sat_dt = datetime.combine(jack["saturday"]["date"], datetime.strptime("18:00", "%H:%M").time()).replace(
            tzinfo=timezone.utc
        )

        gen_tue, _ = score_general(tue_dt)
        per_tue, _ = score_personal(birth_dt, tue_dt, lat, lon)
        comb_tue = (gen_tue + per_tue) / 2.0

        gen_sat, _ = score_general(sat_dt)
        per_sat, _ = score_personal(birth_dt, sat_dt, lat, lon)
        comb_sat = (gen_sat + per_sat) / 2.0

        trend_dates = []
        trend_scores = []
        for i in range(14):
            d = query_date + timedelta(days=i)
            dt_i = datetime.combine(d, datetime.strptime("12:00", "%H:%M").time()).replace(
                tzinfo=timezone.utc
            )
            g, _ = score_general(dt_i)
            p, _ = score_personal(birth_dt, dt_i, lat, lon)
            trend_dates.append(d.strftime("%d.%m."))
            trend_scores.append(round((g + p) / 2.0, 1))

    st.markdown("## Ergebnis")

    b1, b2, b3 = st.columns(3)
    with b1:
        st.info(f"🌙 {phase_label}")
    with b2:
        if merc_rx:
            st.warning("☿ Merkur rückläufig")
        else:
            st.success("☿ Merkur direktläufig")
    with b3:
        st.info(f"📍 {city_choice}")

    st.markdown(
        f"""
        <div style="text-align:center; padding:1.2rem; border-radius:12px;
                    background:linear-gradient(135deg,#f5f5f5,#e8f5e9);
                    border:2px solid {score_color(comb_score)}; margin:1rem 0;">
            <div style="font-size:1.05rem; color:#555;">Kombinierter AstroScore</div>
            <div style="font-size:3rem; font-weight:800; color:{score_color(comb_score)}; line-height:1.1;">
                {comb_score:.1f} %
            </div>
            <div style="font-size:1.8rem;">{luck_symbol(comb_score)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns(2)
    with c1:
        st.markdown(
            f"""
            <div style="text-align:center; padding:0.9rem; border-radius:10px; background:#fafafa; border:1px solid #ddd;">
                <div style="font-size:0.9rem; color:#666;">Allgemein</div>
                <div style="font-size:1.8rem; font-weight:700; color:{score_color(gen_score)};">{gen_score:.1f} %</div>
                <div>{luck_symbol(gen_score)}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f"""
            <div style="text-align:center; padding:0.9rem; border-radius:10px; background:#fafafa; border:1px solid #ddd;">
                <div style="font-size:0.9rem; color:#666;">Persönlich</div>
                <div style="font-size:1.8rem; font-weight:700; color:{score_color(per_score)};">{per_score:.1f} %</div>
                <div>{luck_symbol(per_score)}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("---")
    st.subheader("AstroWeather – Ziehungen")

    diff = comb_sat - comb_tue
    if abs(diff) < 1.5:
        cmp_text = "Dienstag und Samstag liegen nahezu gleich."
    elif diff > 0:
        cmp_text = f"**Samstag** liegt **{diff:.1f} Punkte** höher als Dienstag."
    else:
        cmp_text = f"**Dienstag** liegt **{abs(diff):.1f} Punkte** höher als Samstag."
    st.markdown(cmp_text)

    tcol, scol = st.columns(2)
    with tcol:
        st.markdown(
            f"""
            <div style="text-align:center; padding:1rem; border-radius:10px;
                        background:#fff8e1; border:2px solid {score_color(comb_tue)};">
                <div style="font-size:0.85rem; color:#666;">Dienstag {jack['tuesday']['date'].strftime('%d.%m.%Y')}</div>
                <div style="font-size:1.5rem; font-weight:800; color:{score_color(comb_tue)};">{comb_tue:.1f} %</div>
                <div>{luck_symbol(comb_tue)}</div>
                <div style="font-size:0.8rem; color:#555; margin-top:0.3rem;">
                    ≈ {jack['tuesday']['jackpot_mio']} Mio. € · {jack['tuesday']['tips_mio']} Mio. Tipps
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with scol:
        st.markdown(
            f"""
            <div style="text-align:center; padding:1rem; border-radius:10px;
                        background:#e3f2fd; border:2px solid {score_color(comb_sat)};">
                <div style="font-size:0.85rem; color:#666;">Samstag {jack['saturday']['date'].strftime('%d.%m.%Y')}</div>
                <div style="font-size:1.5rem; font-weight:800; color:{score_color(comb_sat)};">{comb_sat:.1f} %</div>
                <div>{luck_symbol(comb_sat)}</div>
                <div style="font-size:0.8rem; color:#555; margin-top:0.3rem;">
                    ≈ {jack['saturday']['jackpot_mio']} Mio. € · {jack['saturday']['tips_mio']} Mio. Tipps
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("---")
    st.subheader("Score-Verlauf (14 Tage)")
    import pandas as pd

    df_trend = pd.DataFrame({"Datum": trend_dates, "Kombinierter Score %": trend_scores})
    st.line_chart(df_trend.set_index("Datum"))
    best_i = trend_scores.index(max(trend_scores))
    st.caption(
        f"Höchster Wert im Fenster: **{trend_scores[best_i]} %** am {trend_dates[best_i]} "
        f"(rel. zum Abfrage-Datum)."
    )

    with st.expander("Allgemeine Faktoren (gewichtet)"):
        st.text(format_reasons(gen_reasons))
    with st.expander("Persönliche Faktoren (gewichtet)"):
        st.text(format_reasons(per_reasons))

    st.markdown("---")
    export_text = f"""AstroLotto Score – Export
Abfrage: {query_date.isoformat()}
Geburt: {birth_date.isoformat()} {birth_time.strftime('%H:%M')} · {city_choice}

Kombinierter Score: {comb_score:.1f} %
Allgemein: {gen_score:.1f} %
Persönlich: {per_score:.1f} %

Mondphase: {phase_label}
Merkur: {"rückläufig" if merc_rx else "direktläufig"}

Dienstag {jack['tuesday']['date'].isoformat()}: {comb_tue:.1f} %
Samstag {jack['saturday']['date'].isoformat()}: {comb_sat:.1f} %

Vergleich: {cmp_text.replace('**', '')}

Allgemeine Faktoren:
{format_reasons(gen_reasons)}

Persönliche Faktoren:
{format_reasons(per_reasons)}

14-Tage-Verlauf:
""" + "\n".join(f"  {d}: {s} %" for d, s in zip(trend_dates, trend_scores))

    st.download_button(
        "Ergebnis als Text exportieren",
        data=export_text.encode("utf-8"),
        file_name=f"astrolotto_{query_date.isoformat()}.txt",
        mime="text/plain",
        use_container_width=True,
    )

    st.caption(
        "Ephemeriden: skyfield + JPL DE421 · Häuser: Ganz-Zeichen-Näherung · "
        "Jackpot/Tipps: Platzhalter · Nur Unterhaltung"
    )

else:
    st.info("Daten eingeben und **Score berechnen** klicken.")

with st.sidebar:
    st.header("Regelwerk")
    st.markdown(
        """
        **50 % Allgemein + 50 % Persönlich**

        Jupiter, Uranus, Venus, Mondphase,
        Merkur Rx, Saturn, Part of Fortune,
        grobe 5./8./11.-Haus-Aktivierung.

        **AstroWeather:** Score für Di- & Sa-Abend.
        **Verlauf:** 14 Tage ab Abfrage-Datum.
        """
    )
    st.markdown("---")
    if st.session_state.profiles:
        st.caption(f"{len(st.session_state.profiles)} Profile in dieser Sitzung")
        if st.button("Profile löschen"):
            st.session_state.profiles = []
            st.rerun()
    st.caption("Nur zur Unterhaltung.")
