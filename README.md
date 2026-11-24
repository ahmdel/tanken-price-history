# tanken-price-history

Hourly fuel-price history of big cities for the **Tanken** app: how did prices change over the day and the week
in the last 30 days? The app downloads `data/index.json`, picks the nearest big city of the user's country and
shows its cheapest hour and weekday (and says which city it is).

* `cities.json` – the cities (add one, and the next hourly run starts its history)
* `collector/collect.py` – reads the official price source of each country once an hour (GitHub Action `collect.yml`)
* `data/index.json` – list of cities with their fuels / number of samples / start of the history
* `data/<country>/<city>.json` – `{"fuels": {"e5": [[unix_seconds, price], ...], "diesel": [...]}}`, last 30 days

Each point is the mean of the 3 cheapest station prices within `radius_km` of the city centre (same definition as the app).

## Sources and credits
* DE – Tankerkönig / MTS-K, <https://creativecommons.tankerkoenig.de>, CC BY 4.0 (needs the repository secret `TANKERKOENIG_KEY`)
* AT – E-Control Spritpreisrechner, <https://api.e-control.at>
* FR – Prix des carburants en France, <https://data.economie.gouv.fr> (Licence Ouverte 2.0)
* ES – MITECO / Geoportal gasolineras, <https://sedeaplicaciones.minetur.gob.es>

No personal data is collected or stored: only one price number per city, fuel and hour.
