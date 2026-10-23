CREATE TABLE template_module_hours (service text NOT NULL, template_id text NOT NULL, module_code text NOT NULL,
                                    day date NOT NULL, hour smallint NOT NULL, count bigint NOT NULL,
                                    PRIMARY KEY (service, template_id, module_code, day, hour)) PARTITION BY RANGE (day);
CREATE TABLE template_modules (service text NOT NULL, template_id text NOT NULL, module_code text NOT NULL,
                               first_seen timestamptz NOT NULL, last_seen timestamptz NOT NULL,
                               PRIMARY KEY (service, template_id, module_code));
CREATE INDEX template_modules_module ON template_modules (service, module_code);

-- migrate() runs before ensure_partitions(): create the partitions the seed needs, named as db.ensure_partitions does
DO $$
DECLARE d date;
BEGIN
    FOR d IN SELECT DISTINCT (minute AT TIME ZONE 'Asia/Ho_Chi_Minh')::date FROM alert_minutes
             WHERE day >= current_date - 8 AND minute >= now() - interval '7 days' AND key NOT LIKE '*%' AND key NOT LIKE 'doc:%' AND key NOT LIKE '%:unparsed'
    LOOP
        EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF template_module_hours FOR VALUES FROM (%L) TO (%L)',
                       'template_module_hours_' || to_char(d, 'YYYYMMDD'), d, d + 1);
    END LOOP;
END $$;

INSERT INTO template_module_hours (service, template_id, module_code, day, hour, count)
SELECT service, key, module_code, (minute AT TIME ZONE 'Asia/Ho_Chi_Minh')::date,
       extract(hour FROM minute AT TIME ZONE 'Asia/Ho_Chi_Minh')::smallint, sum(count)
FROM alert_minutes
WHERE day >= current_date - 8 AND minute >= now() - interval '7 days' AND key NOT LIKE '*%' AND key NOT LIKE 'doc:%' AND key NOT LIKE '%:unparsed'
GROUP BY 1, 2, 3, 4, 5;

INSERT INTO template_modules (service, template_id, module_code, first_seen, last_seen)
SELECT service, key, module_code, min(minute), max(minute)
FROM alert_minutes WHERE key NOT LIKE '*%' AND key NOT LIKE 'doc:%' AND key NOT LIKE '%:unparsed'
GROUP BY 1, 2, 3;
