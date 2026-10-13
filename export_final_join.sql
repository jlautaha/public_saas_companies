-- SQL inside DuckDB
COPY (SELECT * FROM final_join) 
TO 'saas_public_companies_metrics.csv' (HEADER, DELIMITER ',');