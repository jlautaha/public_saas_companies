# SaaS Public Companies Metrics

This project builds a dataset of efficiency metrics for public SaaS companies. The data feeds a Tableau dashboard.

The financial data comes from SEC EDGAR filings. The stock prices come from Yahoo Finance. Claude reads each company's quarterly earnings press release and tags themes like growth drivers, guidance changes, AI usage, and headwinds.

## How the pipeline works

1. `fetch_sec_data.py` pulls quarterly financials from SEC EDGAR.
2. `fetch_historical_stock_prices.py` pulls daily stock prices from Yahoo Finance.
3. `saas_pr_analysis.py` sends each earnings press release to Claude for analysis. The prompt lives in `saas_analysis_prompts.py`.
4. The SQL files run in DuckDB in this order:
   - `sec_data_cleaned.sql` turns the raw filings into clean quarterly values.
   - `saas_companies_metrics.sql` calculates margins, growth rates, Rule of 40, and the magic number.
   - `final_join.sql` combines the metrics with stock prices and the press release analysis.
   - `export_final_join.sql` writes the final table to `saas_public_companies_metrics.csv`.

The list of companies lives in `saas_companies.csv`.

## Setup

Install the Python packages:

```
pip install requests pandas pyarrow yfinance anthropic python-dotenv beautifulsoup4 duckdb
```

The press release script needs an Anthropic API key. Create a file named `project.env` in the project folder with this line:

```
ANTHROPIC_API_KEY=your_key_here
```

The `project.env` file is listed in `.gitignore`. Git will never upload the file.
