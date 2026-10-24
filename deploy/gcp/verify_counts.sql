-- One row per check, run on both the source (Render) and the copy (Cloud
-- SQL) by scripts/gcp/transfer.sh: every table's row count, and per
-- business the number and value of invoices, counter bills and notes, and
-- each numbering series' next number. Any difference stops the cutover.
SELECT 'rows:' || table_name AS item,
       (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I', table_name), false, true, '')))[1]::text AS value
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE' AND table_name NOT IN ('web_sessions', 'alembic_version')
UNION ALL
SELECT 'invoices:' || tenant_id, count(*) || '/' || coalesce(sum(grand_total), 0) FROM invoices GROUP BY tenant_id
UNION ALL
SELECT 'pos_bills:' || tenant_id, count(*) || '/' || coalesce(sum(grand_total), 0) FROM pos_bills GROUP BY tenant_id
UNION ALL
SELECT 'notes:' || tenant_id, count(*) || '/' || coalesce(sum(grand_total), 0) FROM credit_debit_notes GROUP BY tenant_id
UNION ALL
SELECT 'series:' || id, next_number::text FROM invoice_series
UNION ALL
SELECT 'schema', version_num FROM alembic_version
ORDER BY 1;
