\set ON_ERROR_STOP on

DO $setup$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'teslamate_mcp') THEN
    CREATE ROLE teslamate_mcp LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END
$setup$;

SELECT format(
  'ALTER ROLE teslamate_mcp PASSWORD %L',
  rtrim(pg_read_file('/tmp/teslamate_mcp_db_password'), E' \t\n\r')
) \gexec

ALTER ROLE teslamate_mcp SET default_transaction_read_only = on;
ALTER ROLE teslamate_mcp SET statement_timeout = '5s';
REVOKE ALL ON DATABASE teslamate FROM teslamate_mcp;
GRANT CONNECT ON DATABASE teslamate TO teslamate_mcp;

DROP SCHEMA IF EXISTS teslamate_mcp CASCADE;
CREATE SCHEMA teslamate_mcp AUTHORIZATION teslamate;

CREATE VIEW teslamate_mcp.schema_info AS
SELECT max(version) AS migration_version
FROM public.schema_migrations;

CREATE VIEW teslamate_mcp.vehicles AS
SELECT id, name, model, marketing_name, trim_badging, exterior_color,
       wheel_type, spoiler_type, efficiency, display_priority,
       inserted_at, updated_at
FROM public.cars;

CREATE VIEW teslamate_mcp.settings AS
SELECT unit_of_length::text AS unit_of_length,
       unit_of_temperature::text AS unit_of_temperature,
       preferred_range::text AS preferred_range,
       unit_of_pressure::text AS unit_of_pressure,
       language
FROM public.settings;

CREATE VIEW teslamate_mcp.positions AS
SELECT id, car_id, drive_id, date, latitude, longitude, elevation, speed,
       power, odometer, battery_level, usable_battery_level,
       ideal_battery_range_km, rated_battery_range_km,
       est_battery_range_km, outside_temp, inside_temp,
       is_climate_on, tpms_pressure_fl, tpms_pressure_fr,
       tpms_pressure_rl, tpms_pressure_rr
FROM public.positions;

CREATE VIEW teslamate_mcp.latest_positions AS
SELECT p.id, p.car_id, p.drive_id, p.date, p.latitude, p.longitude,
       p.elevation, p.speed, p.power, p.odometer, p.battery_level,
       p.usable_battery_level, p.ideal_battery_range_km,
       p.rated_battery_range_km, p.est_battery_range_km,
       p.outside_temp, p.inside_temp, p.is_climate_on,
       p.tpms_pressure_fl, p.tpms_pressure_fr,
       p.tpms_pressure_rl, p.tpms_pressure_rr
FROM public.cars c
JOIN LATERAL (
  SELECT pos.*
  FROM public.positions pos
  WHERE pos.car_id = c.id AND pos.ideal_battery_range_km IS NOT NULL
  ORDER BY pos.date DESC
  LIMIT 1
) p ON true;

CREATE VIEW teslamate_mcp.states AS
SELECT id, car_id, state::text AS state, start_date, end_date
FROM public.states;

CREATE VIEW teslamate_mcp.drives AS
SELECT d.id, d.car_id, d.start_date, d.end_date, d.distance, d.duration_min,
       d.speed_max, d.power_max, d.power_min, d.outside_temp_avg,
       d.inside_temp_avg, d.start_km, d.end_km,
       d.start_ideal_range_km, d.end_ideal_range_km,
       d.start_rated_range_km, d.end_rated_range_km,
       d.ascent, d.descent,
       sp.latitude AS start_latitude, sp.longitude AS start_longitude,
       ep.latitude AS end_latitude, ep.longitude AS end_longitude,
       sg.name AS start_geofence, sa.city AS start_city,
       eg.name AS end_geofence, ea.city AS end_city,
       sp.battery_level AS start_battery_level,
       ep.battery_level AS end_battery_level,
       sp.usable_battery_level AS start_usable_battery_level,
       ep.usable_battery_level AS end_usable_battery_level,
       COALESCE(sg.name, NULLIF(sa.name, ''),
                NULLIF(concat_ws(', ', NULLIF(concat_ws(' ', sa.road, sa.house_number), ''), sa.city), '')) AS start_address,
       COALESCE(eg.name, NULLIF(ea.name, ''),
                NULLIF(concat_ws(', ', NULLIF(concat_ws(' ', ea.road, ea.house_number), ''), ea.city), '')) AS end_address,
       c.efficiency
FROM public.drives d
JOIN public.cars c ON c.id = d.car_id
LEFT JOIN public.positions sp ON sp.id = d.start_position_id
LEFT JOIN public.positions ep ON ep.id = d.end_position_id
LEFT JOIN public.addresses sa ON sa.id = d.start_address_id
LEFT JOIN public.addresses ea ON ea.id = d.end_address_id
LEFT JOIN public.geofences sg ON sg.id = d.start_geofence_id
LEFT JOIN public.geofences eg ON eg.id = d.end_geofence_id;

CREATE VIEW teslamate_mcp.charging_sessions AS
SELECT cp.id, cp.car_id, cp.start_date, cp.end_date, cp.duration_min,
       cp.charge_energy_added, cp.charge_energy_used, cp.cost,
       cp.start_battery_level, cp.end_battery_level,
       cp.start_ideal_range_km, cp.end_ideal_range_km,
       cp.start_rated_range_km, cp.end_rated_range_km,
       cp.outside_temp_avg,
       p.latitude, p.longitude, p.odometer,
       g.name AS geofence, a.city AS city,
       COALESCE(g.name, NULLIF(a.name, ''),
                NULLIF(concat_ws(', ', NULLIF(concat_ws(' ', a.road, a.house_number), ''), a.city), '')) AS address,
       stats.fast_charger_present, stats.fast_charger_brand,
       stats.fast_charger_type, stats.charger_power_max,
       stats.is_dc
FROM public.charging_processes cp
LEFT JOIN public.positions p ON p.id = cp.position_id
LEFT JOIN public.addresses a ON a.id = cp.address_id
LEFT JOIN public.geofences g ON g.id = cp.geofence_id
LEFT JOIN LATERAL (
  SELECT bool_or(ch.fast_charger_present) AS fast_charger_present,
         max(ch.fast_charger_brand) AS fast_charger_brand,
         max(ch.fast_charger_type) AS fast_charger_type,
         max(ch.charger_power) AS charger_power_max,
         bool_or(ch.fast_charger_present OR ch.charger_phases IS NULL) AS is_dc
  FROM public.charges ch
  WHERE ch.charging_process_id = cp.id
) stats ON true;

CREATE VIEW teslamate_mcp.charge_samples AS
SELECT id, charging_process_id, date, battery_level, usable_battery_level,
       charge_energy_added, charger_actual_current, charger_phases,
       charger_pilot_current, charger_power, charger_voltage,
       fast_charger_present, outside_temp
FROM public.charges;

CREATE VIEW teslamate_mcp.updates AS
SELECT id, car_id, start_date, end_date, version
FROM public.updates;

REVOKE ALL ON SCHEMA public FROM teslamate_mcp;
GRANT USAGE ON SCHEMA teslamate_mcp TO teslamate_mcp;
GRANT SELECT ON ALL TABLES IN SCHEMA teslamate_mcp TO teslamate_mcp;
