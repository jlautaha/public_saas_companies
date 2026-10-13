import os
import time
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv("project.env")

HEADERS = {"User-Agent": os.environ["SEC_USER_AGENT"]}

def get_sec_cik_mapping() -> dict:
    """Fetch the official SEC ticker-to-CIK mapping dictionary."""
    url = "https://www.sec.gov/files/company_tickers.json"
    response = requests.get(url, headers=HEADERS)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to fetch CIK mapping: HTTP {response.status_code}")
    
    data = response.json()
    ticker_to_cik = {}
    for entry in data.values():
        ticker_to_cik[entry["ticker"].upper()] = str(entry["cik_str"]).zfill(10)
    return ticker_to_cik

def fetch_sec_company_facts(cik: str) -> dict:
    """Fetch the raw XBRL JSON fact tree for a zero-padded CIK number."""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    response = requests.get(url, headers=HEADERS)
    if response.status_code == 200:
        return response.json()
    print(f"Warning: Unable to fetch data for CIK {cik} (Status: {response.status_code})")
    return {}

def get_filing_period_ends(facts_json: dict) -> dict:
    """
    Map each filing's accession number to its fiscal period end date, taken as the latest
    end date among that filing's us-gaap duration facts. Used to re-date dei cover-page values,
    which are dated at the cover date (weeks after period end), to the period they belong to.
    """
    period_ends = {}
    for tag_data in facts_json.get("facts", {}).get("us-gaap", {}).values():
        for entries in tag_data.get("units", {}).values():
            for item in entries:
                accn, end = item.get("accn"), item.get("end")
                if accn and end and item.get("start") and end > period_ends.get(accn, ""):
                    period_ends[accn] = end
    return period_ends

def extract_metric_entries(facts_json: dict, taxonomy_tags: list, min_year: int = 2021,
                           dedupe_across_tags: bool = True, filing_period_ends: dict = None) -> list:
    """
    Extract quarterly/annual entries trying prioritized XBRL tags for 2021 onwards.
    Tags default to the us-gaap taxonomy; prefix a tag with "dei:" to read from the dei taxonomy.
    When dedupe_across_tags is True, a fallback tag only contributes (start, end) periods that no
    higher-priority tag covered. Every filing vintage of the winning tag is kept so the SQL view
    can select the latest (restated) value.
    """
    if not facts_json or "facts" not in facts_json:
        return []

    filing_period_ends = filing_period_ends or {}
    covered_periods = set()
    processed = []
    
    for tag_ref in taxonomy_tags:
        taxonomy, _, tag = tag_ref.rpartition(":")
        taxonomy = taxonomy or "us-gaap"
        taxonomy_facts = facts_json["facts"].get(taxonomy, {})

        if tag not in taxonomy_facts or "units" not in taxonomy_facts[tag]:
            continue
        unit_key = next(iter(taxonomy_facts[tag]["units"]), None)
        if not unit_key:
            continue

        tag_periods = set()
        for item in taxonomy_facts[tag]["units"][unit_key]:
            start = item.get("start")
            end = item.get("end")

            # dei cover-page values are dated at the cover date; re-date them to the filing's period end
            if taxonomy == "dei":
                end = filing_period_ends.get(item.get("accn"))
                if not end:
                    continue

            fy = item.get("fy")
            
            # Check for 10-Q/10-K form type and filter for min_year onwards
            is_valid_form = item.get("form") in ["10-Q", "10-K"] and "val" in item
            is_recent = (fy is not None and fy >= min_year) or (str(end) >= f"{min_year}-01-01")
            if not (is_valid_form and is_recent):
                continue

            # Period coverage is decided by (start, end) only; fy/fp describe the filing, not the period
            period_key = (start, end)
            if dedupe_across_tags and period_key in covered_periods:
                continue

            tag_periods.add(period_key)
            processed.append({
                "gaap_tag": tag,
                "form": item.get("form"),
                "fy": item.get("fy"),
                "fp": item.get("fp"),
                "start": start,
                "end": end,
                "val": item["val"],
                "filed": item.get("filed"),
                "frame": item.get("frame")
            })

        # Update coverage only after the whole tag is processed so all of its filing vintages are kept
        covered_periods |= tag_periods
                        
    return processed

def extract_saas_financials(ticker: str, cik: str) -> pd.DataFrame:
    """Extract standard income statement, balance sheet, and cash flow metrics for a given ticker."""
    facts = fetch_sec_company_facts(cik)
    if not facts:
        return pd.DataFrame()

    filing_period_ends = get_filing_period_ends(facts)

    # Taxonomy mapping with extended fallback tags to account for variations across disclosures
    metric_tags = {
        "Revenue": [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet",
            "OperatingRevenues"
        ],
        "GrossProfit": [
            "GrossProfit"
        ],
        "OperatingIncome": [
            "OperatingIncomeLoss"
        ],
        "ResearchAndDevelopment": [
            "ResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"
        ],
        "SalesAndMarketing": [
            # NOTE: SG&A is an intentional fallback for companies that don't break out S&M.
            # For those companies this metric includes G&A, so S&M-based ratios (e.g. CAC-style
            # efficiency metrics) will be overstated relative to companies reporting S&M directly.
            # The gaap_tag column identifies which companies/periods use the SG&A fallback.
            "SellingAndMarketingExpense",
            "SellingGeneralAndAdministrativeExpense"
        ],
        "OperatingCashFlow": [
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            "NetCashProvidedByUsedInOperatingActivities"
        ],
        "CapEx": [
            # Not deduped across tags: component tags are summed in the SQL view, and
            # PaymentsToAcquireProductiveAssets (a total) is used only when no component exists
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
            "PaymentsToAcquireOtherProductiveAssets",
            "PaymentsToDevelopSoftware",
            "PaymentsToAcquireMachineryAndEquipment"
        ],
        "CashAndCashEquivalents": [
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"
        ],
        "TotalDebt": [
            # Total debt (current + noncurrent) tags first; LongTermDebtNoncurrent excludes the
            # current portion, so it is a last-resort fallback. ShortTermBorrowings was removed.
            "LongTermDebt",
            "LongTermDebtAndCapitalLeaseObligations",
            "ConvertibleNotesPayable",
            "LongTermDebtNoncurrent"
        ],
        "SharesOutstanding": [
            # Cover-page count (dei taxonomy), re-dated to the filing's period end
            "dei:EntityCommonStockSharesOutstanding",
            "CommonStockSharesOutstanding"
        ]
    }

    records = []
    for metric_name, tags in metric_tags.items():
        entries = extract_metric_entries(
            facts, tags, min_year=2021,
            dedupe_across_tags=(metric_name != "CapEx"),
            filing_period_ends=filing_period_ends
        )
        for entry in entries:
            entry["ticker"] = ticker
            entry["metric"] = metric_name
            records.append(entry)

    return pd.DataFrame(records)

# Pipeline Execution
if __name__ == "__main__":
    saas_tickers = (
        pd.read_csv("saas_companies.csv")["Ticker"]
        .dropna()
        .str.strip()
        .str.upper()
        .tolist()
    )

    print("Fetching SEC CIK mappings...")
    cik_map = get_sec_cik_mapping()
    
    extracted_dfs = []
    
    for ticker in saas_tickers:
        cik = cik_map.get(ticker)
        if not cik:
            print(f"Skipping {ticker}: Symbol not currently found in SEC CIK registry.")
            continue
            
        print(f"Extracting financials for {ticker} (CIK: {cik})...")
        df_ticker = extract_saas_financials(ticker, cik)
        if not df_ticker.empty:
            extracted_dfs.append(df_ticker)
            
        # SEC EDGAR rate limit compliance (max 10 requests/sec)
        time.sleep(0.12)

    if extracted_dfs:
        raw_dataset = pd.concat(extracted_dfs, ignore_index=True)
        # Store output locally in columnar Parquet format
        raw_dataset.to_parquet("raw_sec_saas_financials.parquet", index=False)
        print(f"Completed! Extracted {len(raw_dataset)} financial facts across {len(extracted_dfs)} companies.")