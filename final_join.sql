CREATE OR REPLACE VIEW final_join AS

WITH prices AS (
    SELECT ticker,
        "close" AS close_price,
        adj_close AS adj_close_price,
        CAST(date AS DATE) AS close_date,
        most_recent_market_cap
    FROM 'raw_yahoo_historical_prices.parquet'
),

latest_prices AS (
    SELECT ticker,
        close_price AS most_recent_close_price,
        close_date AS most_recent_close_date,
        most_recent_market_cap
    FROM prices
    QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY close_date DESC) = 1
),

press_releases AS (
    SELECT *,
        'CY' || CAST(EXTRACT(YEAR FROM CAST(period_end AS DATE) - INTERVAL 45 DAYS) AS VARCHAR)
             || 'Q' || CAST(EXTRACT(QUARTER FROM CAST(period_end AS DATE) - INTERVAL 45 DAYS) AS VARCHAR) AS calendar_quarter
    FROM 'press_release_analysis.parquet'
),

metrics AS (
    SELECT * EXCLUDE (fiscal_period_end),
        fiscal_period_end AS quarter_end_date,
        (fiscal_period_end + INTERVAL 30 DAY)::DATE AS quarter_end_plus_30
    FROM saas_companies_metrics
    WHERE calendar_year > 2022
),

onion AS (
    SELECT a.*,
        CASE WHEN DATEDIFF('day', qe.close_date, a.quarter_end_date) <= 4
            THEN qe.close_price END AS quarter_end_close_price,
        CASE WHEN DATEDIFF('day', qe.close_date, a.quarter_end_date) <= 4
            THEN qe.adj_close_price END AS quarter_end_adj_close_price,
        CASE WHEN DATEDIFF('day', p30.close_date, a.quarter_end_plus_30) <= 4
             THEN p30.adj_close_price END AS adj_close_price_30d_after
    FROM metrics a
    ASOF LEFT JOIN prices qe
        ON a.ticker = qe.ticker
        AND a.quarter_end_date >= qe.close_date
    ASOF LEFT JOIN prices p30
        ON a.ticker = p30.ticker
        AND a.quarter_end_plus_30 >= p30.close_date
)

SELECT c.Company as company,
    c.Category as category,
    c."Growth Stage" as growth_stage,
    c.Size as size,
    c.product_scope as moat_product_scope,
    c.data_role as moat_data_role,
    c.pricing_model as moat_pricing_model,
    c.headcount_linked as moat_headcount_linked,
    c.segment as moat_segment,
    a.*,
    lp.most_recent_market_cap,
    lp.most_recent_close_price,
    lp.most_recent_close_date,
    pr.* EXCLUDE (ticker, calendar_quarter)
FROM onion a
LEFT JOIN latest_prices lp
    ON a.ticker = lp.ticker
LEFT JOIN 'saas_companies.csv' c
    ON a.ticker = UPPER(TRIM(c.Ticker))
LEFT JOIN press_releases pr
    ON a.ticker = pr.ticker
    AND a.calendar_quarter = pr.calendar_quarter;