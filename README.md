# AstroLotto Score v3

Experimentelles Scoring-System (Unterhaltung).

## Features
- skyfield + JPL DE421 Ephemeriden
- 80+ Städte mit Zeitzonen
- Kombinierter / Allgemeiner / Persönlicher Score
- AstroWeather für Dienstag- & Samstagsziehung
- 14-Tage Score-Verlauf
- Gewichtete Begründungen
- Mondphase- & Merkur-Badges
- Profil-Historie (Session)
- Text-Export
- Grobe Ganz-Zeichen-Häuser (5/8/11)

## Start
```bash
pip install -r requirements.txt
streamlit run app.py
```

Beim ersten Start lädt skyfield `de421.bsp` (~17 MB).

**Nur zur Unterhaltung – keine Gewinngarantie.**
