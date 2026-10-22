ALTER TABLE modules DROP CONSTRAINT modules_pkey;
ALTER TABLE modules ADD PRIMARY KEY (service, module_code);
CREATE INDEX modules_code ON modules (module_code);
