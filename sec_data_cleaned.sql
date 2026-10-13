CREATE OR REPLACE VIEW sec_data_cleaned AS

WITH sec_base AS (
    SELECT 
        ticker,
        metric,
        CASE WHEN gaap_tag = 'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations'
                  THEN 'NetCashProvidedByUsedInOperatingActivities'
             WHEN metric = 'Revenue' THEN 'Revenues'
             ELSE gaap_tag 
        END AS gaap_tag,
        form,
        CAST(start AS DATE) AS start_date,
        CAST("end" AS DATE) AS end_date,
        val,
        CAST(filed AS DATE) AS filed_date,
        CASE 
            WHEN start IS NULL THEN NULL 
            ELSE DATEDIFF('day', CAST(start AS DATE), CAST("end" AS DATE)) 
        END AS duration_days
    FROM 'raw_sec_saas_financials.parquet'
    WHERE form IN ('10-Q', '10-K')
),

-- Step 1: Classify duration profiles by day counts
sec_classified AS (
    SELECT 
        *,
        CASE 
            WHEN start_date IS NULL THEN 'instantaneous'
            WHEN duration_days BETWEEN 80 AND 105 THEN 'quarter'
            WHEN duration_days BETWEEN 170 AND 195 THEN '6mo'
            WHEN duration_days BETWEEN 260 AND 285 THEN '9mo'
            WHEN duration_days BETWEEN 350 AND 380 THEN 'annual'
            ELSE 'other'
        END AS period_type,
        end_date - INTERVAL 45 DAYS AS shifted_date
    FROM sec_base
),

-- Step 2: Deduplicate to retain the most recent filing per timeframe (per tag, so CapEx components survive)
sec_deduped AS (
    SELECT *
    FROM sec_classified
    WHERE period_type != 'other'
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY ticker, metric, gaap_tag, start_date, end_date, period_type 
        ORDER BY filed_date DESC
    ) = 1
),

-- Step 3: Fetch prior cumulative values strictly within identical fiscal start dates and tags
-- (partitioning by gaap_tag ensures YTD subtraction never mixes values from different tags)
lag_calculations AS (
    SELECT 
        ticker,
        metric,
        gaap_tag,
        form,
        start_date,
        end_date,
        shifted_date,
        val,
        filed_date,
        period_type,
        LAG(period_type) OVER w AS lag_period_type,
        LAG(val) OVER w AS lag_val,
        LAG(end_date) OVER w AS lag_end_date
    FROM sec_deduped
    WINDOW w AS (
        PARTITION BY ticker, metric, gaap_tag, start_date 
        ORDER BY end_date
    )
),

-- Step 4: Perform sequential subtractive checks & output validation
discrete_financials AS (
    SELECT 
        ticker,
        metric,
        gaap_tag,
        form,
        start_date,
        end_date,
        shifted_date,
        filed_date,
        period_type,
        lag_period_type,
        val AS cumulative_val,
        
        -- Discrete quarterly derivation logic
        CASE 
            -- Balance Sheet point-in-time
            WHEN period_type = 'instantaneous' 
                THEN val
                
            -- Discrete 3mo quarter (Q1 YTD or standalone 3mo)
            WHEN period_type = 'quarter' 
                THEN val
                
            -- 6mo YTD: Requires previous row in start_date window to be 3mo quarter
            WHEN period_type = '6mo' AND lag_period_type = 'quarter' 
                THEN val - lag_val
                
            -- 9mo YTD: Requires previous row in start_date window to be 6mo YTD
            WHEN period_type = '9mo' AND lag_period_type = '6mo' 
                THEN val - lag_val
                
            -- 12mo Annual: Requires previous row in start_date window to be 9mo YTD
            WHEN period_type = 'annual' AND lag_period_type = '9mo' 
                THEN val - lag_val
                
            -- Sequence mismatch/gap safeguard
            ELSE NULL 
        END AS discrete_val,

        -- Audit status flag
        CASE 
            WHEN period_type = 'instantaneous' THEN 'instantaneous'
            WHEN period_type = 'quarter' THEN 'discrete_quarter'
            WHEN period_type = '6mo' AND lag_period_type = 'quarter' THEN 'valid_ytd_subtraction'
            WHEN period_type = '9mo' AND lag_period_type = '6mo' THEN 'valid_ytd_subtraction'
            WHEN period_type = 'annual' AND lag_period_type = '9mo' THEN 'valid_ytd_subtraction'
            ELSE 'invalid_sequence_gap'
        END AS sequence_status
    FROM lag_calculations
),

-- Step 5: One value per tag per period end. 10-Qs report both the 3mo quarter and the YTD figure,
-- so Q2/Q3 appear twice (direct and derived); prefer the directly reported quarter
per_tag AS (
    SELECT *
    FROM discrete_financials
    WHERE discrete_val IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY ticker, metric, gaap_tag, end_date
        ORDER BY CASE WHEN sequence_status = 'valid_ytd_subtraction' THEN 1 ELSE 0 END,
                 filed_date DESC
    ) = 1
),

-- Step 6: CapEx sums its component tags; PaymentsToAcquireProductiveAssets is a total,
-- so it is used only for periods where no component tag exists (avoids double-counting)
capex_flagged AS (
    SELECT 
        *,
        SUM(CASE WHEN gaap_tag <> 'PaymentsToAcquireProductiveAssets' THEN 1 ELSE 0 END)
            OVER (PARTITION BY ticker, end_date) AS n_capex_components
    FROM per_tag
    WHERE metric = 'CapEx'
),

capex_combined AS (
    SELECT 
        ticker,
        metric,
        STRING_AGG(gaap_tag, ' + ' ORDER BY gaap_tag) AS gaap_tag,
        MAX(form) AS form,
        MIN(start_date) AS start_date,
        end_date,
        MAX(shifted_date) AS shifted_date,
        MAX(filed_date) AS filed_date,
        STRING_AGG(DISTINCT period_type, ', ') AS period_type,
        STRING_AGG(DISTINCT lag_period_type, ', ') AS lag_period_type,
        STRING_AGG(DISTINCT sequence_status, ', ') AS sequence_status,
        SUM(cumulative_val) AS cumulative_val,
        SUM(discrete_val) AS discrete_val
    FROM capex_flagged
    WHERE n_capex_components = 0 
       OR gaap_tag <> 'PaymentsToAcquireProductiveAssets'
    GROUP BY ticker, metric, end_date
),

-- Step 7: All other metrics keep one row per period end, again preferring directly reported quarters
other_metrics AS (
    SELECT 
        ticker,
        metric,
        gaap_tag,
        form,
        start_date,
        end_date,
        shifted_date,
        filed_date,
        period_type,
        lag_period_type,
        sequence_status,
        cumulative_val,
        discrete_val
    FROM per_tag
    WHERE metric <> 'CapEx'
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY ticker, metric, end_date
        ORDER BY CASE WHEN sequence_status = 'valid_ytd_subtraction' THEN 1 ELSE 0 END,
                 filed_date DESC
    ) = 1
),

combined AS (
    SELECT * FROM other_metrics
    UNION ALL BY NAME
    SELECT * FROM capex_combined
)

-- Step 8: Final output with normalized calendar quarters
SELECT 
    ticker,
    metric,
    gaap_tag,
    form,
    start_date,
    end_date,
    shifted_date,
    filed_date,
    period_type,
    lag_period_type,
    sequence_status,
    EXTRACT(YEAR FROM shifted_date) AS calendar_year,
    'CY' || CAST(EXTRACT(YEAR FROM shifted_date) AS VARCHAR) 
         || 'Q' || CAST(EXTRACT(QUARTER FROM shifted_date) AS VARCHAR) AS calendar_quarter,
    cumulative_val,
    discrete_val
FROM combined
ORDER BY ticker, metric, end_date ASC;