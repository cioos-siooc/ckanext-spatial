CREATE OR REPLACE FUNCTION public.extent_hexagons(
  z integer, x integer, y integer,
  step integer default 4)
RETURNS bytea AS
$$
WITH bounds AS (
  -- get web mercator tile bounds to given coordinate
  SELECT ST_TileEnvelope(z, x, y) AS geom
), hexes AS (
  -- generate hexgrid within bounds and join with population grid
  SELECT row_number() OVER () AS grid_id,
        h.geom, h.i, h.j,
        count(p.the_geom) AS polycount,
        json_agg(p.title) as titles
   FROM

   bounds b
   JOIN LATERAL
   ST_HexagonGrid(  -- 1. hex size, 2. boundary
          (ST_XMax(b.geom) - ST_XMin(b.geom)) / pow(2, step), b.geom
        ) h ON (true)

   -- do spatial join between our artificial grid and the Geostat grid
   -- the hex grid is in web mercator coordinate reference system (CRS)
   -- it must be tranformed into the same CRS of the population grid (WGS84 - 4326)
   JOIN (select * from package join package_extent on package.id = package_extent.package_id) as p
     ON p.the_geom && ST_Transform(h.geom, 4326)
  GROUP BY h.geom, h.i, h.j
), mvt AS (
  -- processing geometry for vector tiles
  SELECT ST_AsMVTGeom(h.geom, b.geom) AS geom,
         (h.i::text || h.j::text || h.grid_id::text)::int AS grid_id,
         h.polycount,
         h.titles
    FROM hexes h, bounds b
)
-- baking mvt geom, grid_id, polycount, and titles into MVT encoding
SELECT ST_AsMVT(mvt, 'public.extent_hexagons') FROM mvt;
$$
LANGUAGE 'sql' STABLE STRICT PARALLEL SAFE;
