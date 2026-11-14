# data/

`pincode_geo.csv.gz` - one row per Indian pincode (pincode, lat, lng, district,
state, level) for the nearest-warehouse default (app/warehouse/nearest.py).
Built with `flask pincodes-build <file>` from India Post's **All India Pincode
Directory** (with latitude and longitude) on data.gov.in, published under the
**Government Open Data Licence - India** (GODL), which allows use and
redistribution with attribution: "Source: Department of Posts, Ministry of
Communications, Government of India - data.gov.in".

`level`: `own` (the pincode's own offices' average), `district` (its district's
average - its own coordinates were missing or outside India), `area` (the
average of the pincodes sharing its first three digits).

`level` may also be `none` (no point at all - its district and state only).

The deploy loads it into the `pincode_geo` table (`flask pincodes-load`, run by
deploy/migrate.sh).

Current file: built from **Pincode_30052019.csv**, India Post directory dated
30 May 2019 (19,252 pincodes), supplied by the owner on 9 Nov 2026. It carries
district and state but no coordinates, so all rows are level `none`; rebuild
from the directory with latitude/longitude to get distances.
