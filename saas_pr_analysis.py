import os
import json
import re
import time
from datetime import date
from dotenv import load_dotenv

import anthropic
import pandas as pd
import requests
from bs4 import BeautifulSoup

from saas_analysis_prompts import SYSTEM_PROMPT

load_dotenv("project.env")
HEADERS = {"User-Agent": os.environ["SEC_USER_AGENT"]}
MODEL = "claude-sonnet-5-5"
OUTPUT_FILE = "press_release_analysis.parquet"

SAAS_TICKERS = pd.read_csv("saas_companies.csv")["Ticker"].str.strip().str.upper().tolist()

def get_cik_mapping() -> dict:
    url = "https://www.sec.gov/files/company_tickers.json"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    return {
        entry["ticker"].upper(): str(entry["cik_str"]).zfill(10)
        for entry in response.json().values()
    }


def _d(s: str) -> date:
    return date.fromisoformat(s)


def find_earnings_releases(cik: str, n: int = 2, max_gap_days: int = 60,
                           min_period_lag: int = 0, max_period_lag: int = 70) -> list:
    """
    Return up to n most recent Item 2.02 8-Ks that are followed by a 10-Q/10-K.
    An 8-K with no following periodic filing (or an implausible gap) is dropped
    BEFORE the top-n cutoff, so we still get n matched quarters when possible.
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    recent = response.json()["filings"]["recent"]

    periodic = []   # 10-Q / 10-K filings
    eight_ks = []   # 8-Ks with Item 2.02
    for i, form in enumerate(recent["form"]):
        row = {
            "form": form,
            "accession": recent["accessionNumber"][i],
            "filing_date": recent["filingDate"][i],
            "report_date": recent["reportDate"][i],
        }
        if form in ("10-Q", "10-K") and row["report_date"]:
            periodic.append(row)
        elif form == "8-K" and "2.02" in recent["items"][i]:
            eight_ks.append(row)

    periodic.sort(key=lambda r: r["filing_date"])

    # Match each 8-K to the first 10-Q/10-K filed on or after it
    matches = {}  # periodic accession -> (8-K row, periodic row)
    for k in eight_ks:
        k_date = _d(k["filing_date"])
        follower = next((p for p in periodic if _d(p["filing_date"]) >= k_date), None)
        if not follower:
            continue
        if (_d(follower["filing_date"]) - k_date).days > max_gap_days:
            continue
        lag = (k_date - _d(follower["report_date"])).days  # 8-K date vs. period end
        if not (min_period_lag <= lag <= max_period_lag):
            continue
        # One-to-one: if several 8-Ks map to the same 10-Q, keep the latest
        current = matches.get(follower["accession"])
        if current is None or k["filing_date"] > current[0]["filing_date"]:
            matches[follower["accession"]] = (k, follower)

    ordered = sorted(matches.values(), key=lambda m: m[0]["filing_date"], reverse=True)[:n]

    releases = []
    for k, p in ordered:
        accession_nodash = k["accession"].replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}"

        index = requests.get(f"{base}/index.json", headers=HEADERS)
        index.raise_for_status()
        time.sleep(0.12)
        files = [f["name"] for f in index.json()["directory"]["item"]]
        htm = [f for f in files if f.lower().endswith(".htm")]
        main_doc = re.compile(r"^[a-z]+-\d{8}\.htm$")

        exhibits = [f for f in htm if "99" in f]
        if not exhibits:
            keywords = ("ea", "pr", "fy", "q1", "q2", "q3", "q4")
            exhibits = [f for f in htm
                        if not main_doc.match(f.lower())
                        and any(k in f.lower() for k in keywords)]
        if not exhibits:
            raise RuntimeError(f"No press release file found in {base}. Files: {files}")

        releases.append({
            "filing_date": k["filing_date"],
            "report_date": k["report_date"],      # 8-K event date
            "period_end": p["report_date"],       # quarter end, from the 10-Q/10-K
            "periodic_form": p["form"],
            "periodic_filing_date": p["filing_date"],
            "url": f"{base}/{exhibits[0]}",
        })
    return releases


def fetch_release_text(url: str) -> str:
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    text = soup.get_text(separator="\n")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def run_prompt(client: anthropic.Anthropic, ticker: str, release: dict, text: str) -> dict:
    user_message = (
        f"Ticker: {ticker}\n"
        f"Filing date: {release['filing_date']}\n\n"
        f"MOST RECENT PRESS RELEASE TEXT:\n{text}"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = next(b.text for b in response.content if b.type == "text").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    obj, _ = json.JSONDecoder().raw_decode(raw[raw.index("{"):])
    return obj


if __name__ == "__main__":
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    cik_map = get_cik_mapping()

    if os.path.exists(OUTPUT_FILE):
        existing = pd.read_parquet(OUTPUT_FILE)
        done = set(zip(existing["ticker"], existing["period_end"]))
    else:
        existing = pd.DataFrame()
        done = set()

    rows = []
    failures = []

    for ticker in SAAS_TICKERS:
        print(f"Processing {ticker}...")
        try:
            cik = cik_map.get(ticker)
            if not cik:
                raise ValueError("ticker not found in SEC mapping")
            releases = find_earnings_releases(cik, n=12)
            if not releases:
                raise RuntimeError("no 2.02 8-K with a following 10-Q/10-K")
        except Exception as e:
            print(f"  Skipped {ticker}: {type(e).__name__}: {e}")
            failures.append(ticker)
            time.sleep(0.12)
            continue

        for release in releases:
            if (ticker, release["period_end"]) in done:
                print(f"  Already analyzed {ticker} {release['period_end']}, skipping")
                continue
            try:
                time.sleep(0.12)  # SEC rate limit compliance
                text = fetch_release_text(release["url"])
                result = run_prompt(client, ticker, release, text)

                result["ticker"] = ticker
                result["filing_date"] = release["filing_date"]
                result["report_date"] = release["report_date"]
                result["period_end"] = release["period_end"]
                result["periodic_form"] = release["periodic_form"]
                result["source_url"] = release["url"]
                rows.append(result)
            except Exception as e:
                print(f"  Skipped {ticker} {release['filing_date']}: {type(e).__name__}: {e}")
                failures.append(f"{ticker} {release['filing_date']}")
        time.sleep(0.12)

       # Flatten nested answers, e.g. G1 -> G1_answer, G1_evidence
    if rows:
        df = pd.concat([existing, pd.json_normalize(rows, sep="_")], ignore_index=True)
        df.to_parquet(OUTPUT_FILE, index=False)
        print(f"\nAdded {len(rows)} press releases; {len(df)} total in {OUTPUT_FILE}")
    else:
        print("\nNo new press releases to analyze.")

    if failures:
        print(f"Skipped: {', '.join(failures)}")