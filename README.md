# california-hourly-disaggregation

Use publicly available data to predict California disaggregation for transmission projects.

## Project Structure

```
hourly-california-disaggregation/
├── data/
│   ├── raw/           # Original, unmodified source data (gitignored)
│   └── processed/     # Cleaned and feature-engineered data (gitignored)
├── notebooks/         # Jupyter notebooks for exploration and analysis
├── src/
│   ├── data/          # Data ingestion and cleaning scripts
│   ├── features/      # Feature engineering
│   └── models/        # Model training and evaluation
├── tests/             # Unit tests
├── requirements.txt
└── README.md
```

## Data Sources

- [CAISO OASIS](http://oasis.caiso.com/) — hourly load, generation, and transmission data (TBD)
- [EIA Open Data](https://www.eia.gov/opendata/) — energy statistics and forecasts

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -r requirements.txt
```

## Usage

Place raw data files in `data/raw/`, then run notebooks in order from `notebooks/`.
