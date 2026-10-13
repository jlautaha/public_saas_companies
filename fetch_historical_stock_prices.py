import pandas as pd
import yfinance as yf

# Load target SaaS Tickers from CSV
SAAS_TICKERS = pd.read_csv("saas_companies.csv")["Ticker"].str.strip().tolist()

def fetch_company_info(tickers: list) -> dict:
    """Fetches company long names (or short names) and current market cap for a list of tickers."""
    print("Fetching company names and market caps...")
    company_map = {}
    for ticker in tickers:
        try:
            info = yf.Ticker(ticker).info
            company_map[ticker] = {
                "company_name": info.get("longName") or info.get("shortName") or ticker,
                "market_cap": info.get("marketCap"),
            }
        except Exception:
            company_map[ticker] = {"company_name": ticker, "market_cap": None}
    return company_map

def fetch_historical_stock_prices(tickers: list, start_date: str = "2023-01-01") -> pd.DataFrame:
    """
    Downloads historical daily prices for a list of tickers from start_date to present.
    Reshapes data into a clean long-format DataFrame for SQL/DuckDB ingestion.
    """
    # 1. Retrieve company name and market cap mapping
    company_info = fetch_company_info(tickers)

    print(f"Downloading historical price data for {len(tickers)} tickers from {start_date}...")
    
    # Download batch data (auto_adjust=False retains original Close and Adj Close)
    data = yf.download(
        tickers=tickers,
        start=start_date,
        group_by="ticker",
        auto_adjust=False,
        progress=True
    )

    records = []

    # Handle multi-index structure returned by yfinance batch download
    for ticker in tickers:
        if ticker in data.columns.levels[0]:
            df_ticker = data[ticker].dropna(how="all").copy()
            df_ticker.reset_index(inplace=True)
            
            company = company_info.get(ticker, {"company_name": ticker, "market_cap": None})

            for _, row in df_ticker.iterrows():
                # Format Date cleanly to YYYY-MM-DD string
                date_str = row["Date"].strftime("%Y-%m-%d") if pd.notnull(row["Date"]) else None
                
                records.append({
                    "ticker": ticker,
                    "company_name": company["company_name"],
                    "date": date_str,
                    "open": float(row["Open"]) if pd.notnull(row["Open"]) else None,
                    "high": float(row["High"]) if pd.notnull(row["High"]) else None,
                    "low": float(row["Low"]) if pd.notnull(row["Low"]) else None,
                    "close": float(row["Close"]) if pd.notnull(row["Close"]) else None,
                    "adj_close": float(row["Adj Close"]) if pd.notnull(row["Adj Close"]) else None,
                    "volume": int(row["Volume"]) if pd.notnull(row["Volume"]) else None,
                    "most_recent_market_cap": company["market_cap"],
                })

    df_result = pd.DataFrame(records)
    return df_result

if __name__ == "__main__":
    df_history = fetch_historical_stock_prices(SAAS_TICKERS, start_date="2023-01-01")

    # Save output to Parquet and CSV for downstream storage/joins
    df_history.to_parquet("raw_yahoo_historical_prices.parquet", index=False)

    print(f"\nSuccessfully extracted {len(df_history)} daily price records.")
    print(df_history.head(10))