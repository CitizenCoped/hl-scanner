CREATE TABLE alerts (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL,
  coin TEXT NOT NULL,
  z DOUBLE PRECISION NOT NULL,
  ret DOUBLE PRECISION NOT NULL,
  notional DOUBLE PRECISION NOT NULL,
  mid DOUBLE PRECISION NOT NULL,
  bucket_hour SMALLINT NOT NULL
);
CREATE INDEX alerts_ts_coin ON alerts(ts DESC, coin);

CREATE TABLE markouts (
  alert_id BIGINT REFERENCES alerts(id) ON DELETE CASCADE,
  horizon TEXT NOT NULL CHECK (horizon IN ('30s','5m','30m','4h')),
  mid_at_horizon DOUBLE PRECISION,
  recorded_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (alert_id, horizon)
);
