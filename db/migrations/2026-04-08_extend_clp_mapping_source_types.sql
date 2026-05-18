-- Extends curriculum mapping entry source types for constructive-alignment coverage.
-- Run this once in Supabase SQL editor before using WLO/assessment mapping analytics.

ALTER TABLE clp_mapping_entries
DROP CONSTRAINT IF EXISTS clp_mapping_entries_source_type_check;

ALTER TABLE clp_mapping_entries
ADD CONSTRAINT clp_mapping_entries_source_type_check
CHECK (source_type IN ('CO_PO', 'PO_IO', 'WLO_CO', 'ASSESSMENT_CO', 'CO_EXTERNAL'));
