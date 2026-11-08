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

The deploy loads it into the `pincode_geo` table (`flask pincodes-load`, run by
deploy/migrate.sh). Source file and date: recorded in the README's "Nearest
warehouse" note when the file is added.
