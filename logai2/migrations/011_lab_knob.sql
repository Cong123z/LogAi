-- Lab scoring: episodes per knob, one daily row of drawn band arrays. Lab tables only (they hold no data yet).
DROP TABLE lab_episodes;
CREATE TABLE lab_episodes (
  run_id bigint REFERENCES lab_runs ON DELETE CASCADE, method text, knob double precision NOT NULL, service text,
  key text, start_minute int, end_minute int, peak_score double precision, alerted_minutes int,
  PRIMARY KEY (run_id, method, knob, service, key, start_minute));

DROP TABLE lab_bands;
CREATE TABLE lab_bands (          -- one row per drawn key and day: step 1 = 1440 values, step 5 = 288 five-minute means
  run_id bigint REFERENCES lab_runs ON DELETE CASCADE, method text, knob double precision NOT NULL, service text,
  key text, day date, step smallint NOT NULL, counts int[] NOT NULL, expected real[], low real[], high real[],
  flags boolean[] NOT NULL,
  PRIMARY KEY (run_id, method, knob, service, key, day));
