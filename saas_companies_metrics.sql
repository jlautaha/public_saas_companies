CREATE OR REPLACE VIEW saas_companies_metrics AS

WITH pivoted_financials AS (
    -- Step 1: Pivot long metric rows into 1 row per Ticker & Calendar Quarter
    SELECT 
        ticker,
        calendar_quarter,
        MAX(calendar_year) AS calendar_year,
        MAX(shifted_date) AS quarter_end_date,
        MAX(CASE WHEN metric = 'Revenue' THEN discrete_val END) AS revenue,
        MAX(CASE WHEN metric = 'GrossProfit' THEN discrete_val END) AS gross_profit,
        MAX(CASE WHEN metric = 'OperatingIncome' THEN discrete_val END) AS operating_income,
        MAX(CASE WHEN metric = 'SalesAndMarketing' THEN discrete_val END) AS sales_and_marketing,
        MAX(CASE WHEN metric = 'ResearchAndDevelopment' THEN discrete_val END) AS research_and_development,
        MAX(CASE WHEN metric = 'OperatingCashFlow' THEN discrete_val END) AS operating_cash_flow,
        MAX(CASE WHEN metric = 'CapEx' THEN discrete_val END) AS capex,
        MAX(CASE WHEN metric = 'CashAndCashEquivalents' THEN discrete_val END) AS cash_and_cash_equivalents,
        MAX(CASE WHEN metric = 'TotalDebt' THEN discrete_val END) AS total_debt,
        MAX(CASE WHEN metric = 'SharesOutstanding' THEN discrete_val END) AS shares_outstanding
    FROM sec_data_cleaned
    GROUP BY ticker, calendar_quarter
),

base_calculated_metrics AS (
    -- Step 2: Calculate basic single-quarter margins, FCF, and annualized run-rate
    SELECT 
        ticker,
        calendar_year,
        calendar_quarter,
        quarter_end_date,
        
        -- Sequential calendar-quarter index so windows step by quarter, not by row
        -- (a missing quarter no longer shifts t-1 / t-4 / LTM lookbacks)
        (EXTRACT(YEAR FROM quarter_end_date) * 4 + EXTRACT(QUARTER FROM quarter_end_date)) AS quarter_idx,
        
        revenue,
        gross_profit,
        operating_income,
        sales_and_marketing,
        research_and_development,
        operating_cash_flow,
        capex,
        cash_and_cash_equivalents,
        total_debt,
        shares_outstanding,
        
        -- Annualized Revenue Run-Rate (GAAP Proxy for ARR)
        (revenue * 4) AS annualized_run_rate,
        
        -- Free Cash Flow (FCF)
        (operating_cash_flow - capex) AS free_cash_flow,
        
        -- Net Debt
        (total_debt - cash_and_cash_equivalents) AS net_debt,
        
        -- Single-Quarter Margins (%)
        (gross_profit / NULLIF(revenue, 0)) AS gross_margin_pct,
        (operating_income / NULLIF(revenue, 0)) AS operating_margin_pct,
        ((operating_cash_flow - capex) / NULLIF(revenue, 0)) AS fcf_margin_pct,
        (sales_and_marketing / NULLIF(revenue, 0)) AS sm_pct_of_rev,
        (research_and_development / NULLIF(revenue, 0)) AS rd_pct_of_rev
    FROM pivoted_financials
),

lagged_and_ltm_metrics AS (
    -- Step 3: Quarter-indexed windows for prior-period offsets and LTM sums.
    -- RANGE frames on quarter_idx return NULL when the target quarter is missing,
    -- and LTM / 4-quarter sums are NULL unless all 4 quarters are present.
    SELECT 
        *,
        -- Prior Quarter Revenue (t-1)
        MAX(revenue) OVER w_prior_q AS prior_q_revenue,
        
        -- Prior Year Quarter Revenue (t-4)
        MAX(revenue) OVER w_prior_year_q AS prior_year_q_revenue,
        
        -- Sales & Marketing summed over the prior 4 quarters (t-4 through t-1)
        CASE WHEN COUNT(sales_and_marketing) OVER w_prior_4q = 4
             THEN SUM(sales_and_marketing) OVER w_prior_4q
        END AS prior_4q_sm,
        
        -- Trailing Twelve Months (LTM) Revenue
        CASE WHEN COUNT(revenue) OVER w_ltm = 4
             THEN SUM(revenue) OVER w_ltm
        END AS ltm_revenue,
        
        -- Trailing Twelve Months (LTM) Free Cash Flow
        CASE WHEN COUNT(free_cash_flow) OVER w_ltm = 4
             THEN SUM(free_cash_flow) OVER w_ltm
        END AS ltm_fcf
    FROM base_calculated_metrics
    WINDOW
        w_prior_q      AS (PARTITION BY ticker ORDER BY quarter_idx RANGE BETWEEN 1 PRECEDING AND 1 PRECEDING),
        w_prior_year_q AS (PARTITION BY ticker ORDER BY quarter_idx RANGE BETWEEN 4 PRECEDING AND 4 PRECEDING),
        w_prior_4q     AS (PARTITION BY ticker ORDER BY quarter_idx RANGE BETWEEN 4 PRECEDING AND 1 PRECEDING),
        w_ltm          AS (PARTITION BY ticker ORDER BY quarter_idx RANGE BETWEEN 3 PRECEDING AND CURRENT ROW)
)

-- Step 4: Final Calculations for Multi-Period Ratios (YoY, QoQ, Magic Number, Rule of 40)
SELECT 
    ticker,
    calendar_year,
    DATE_TRUNC('quarter', quarter_end_date)::DATE AS calendar_quarter_date,
    calendar_quarter,
    (quarter_end_date + INTERVAL 45 DAY)::DATE AS fiscal_period_end,
    
    -- Absolute Values
    revenue,
    annualized_run_rate,
    ltm_revenue,
    gross_profit,
    operating_income,
    capex,
    free_cash_flow,
    ltm_fcf,
    net_debt,
    shares_outstanding,
    
    -- Margins (%)
    ROUND(gross_margin_pct, 4) AS gross_margin_pct,
    ROUND(operating_margin_pct, 4) AS operating_margin_pct,
    ROUND(fcf_margin_pct, 4) AS fcf_margin_pct,
    ROUND(sm_pct_of_rev, 4) AS sm_pct_of_rev,
    ROUND(rd_pct_of_rev, 4) AS rd_pct_of_rev,
    
    -- Growth Rates (%)
    ROUND(((revenue - prior_q_revenue) / NULLIF(prior_q_revenue, 0)), 4) AS qoq_revenue_growth_pct,
    ROUND(((revenue - prior_year_q_revenue) / NULLIF(prior_year_q_revenue, 0)), 4) AS yoy_revenue_growth_pct,
    
    -- Efficiency Benchmarks
    -- Rule of 40: quarterly YoY revenue growth + LTM FCF margin
    ROUND(
        (((revenue - prior_year_q_revenue) / NULLIF(prior_year_q_revenue, 0)))
        + (ltm_fcf / NULLIF(ltm_revenue, 0)), 
        4
    ) AS rule_of_40,
    
    -- YoY Magic Number: annualized YoY increase in quarterly revenue / S&M over prior 4 quarters
    ROUND(
        ((revenue - prior_year_q_revenue) * 4) / NULLIF(prior_4q_sm, 0), 
        2
    ) AS saas_magic_number

FROM lagged_and_ltm_metrics
ORDER BY ticker, quarter_end_date DESC;