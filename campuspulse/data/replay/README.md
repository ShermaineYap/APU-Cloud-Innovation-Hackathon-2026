# Replay datasets: real measured sensor data

These files hold **real measurements from a published dataset**, not APU readings and not
simulation. The dashboard's **Published dataset (Mons office)** view (`campuspulse/replay.py`) plays
them as their own site: the study's office room, its own sensors and timeline, with CampusPulse's
room rules applied (occupied, CO2 over the limit, lights on with nobody in). They are never shown
as an APU room and never written to the warehouse.

## uci_occupancy_5min.csv

- **Source:** Occupancy Detection dataset, UCI Machine Learning Repository.
  Candanedo, L. (2016). DOI [10.24432/C5X01N](https://doi.org/10.24432/C5X01N).
  Dataset page: <https://archive.ics.uci.edu/dataset/357/occupancy+detection>
- **Paper:** Candanedo, L. M., & Feldheim, V. (2016). Accurate occupancy detection of an office room
  from light, temperature, humidity and CO2 measurements using statistical learning models.
  *Energy and Buildings*, 112, 28–39.
- **Licence:** Creative Commons Attribution 4.0 International (CC BY 4.0).
- **What it measured:** an office room in Mons, Belgium, 2–18 February 2015: temperature (°C),
  relative humidity (%), light (lux) and CO2 (ppm) every minute. Occupancy ground truth came from
  time-stamped pictures taken every minute.
- **What we changed:** merged the three published files, sorted by time, and averaged to 5-minute
  steps (CampusPulse's interval). `occupied` is 1 if the room was occupied at any point in the
  5 minutes. 4,114 rows. No values were altered beyond this averaging.
- **What it does not have:** a head count (only occupied yes/no), AC state, power, or Wi-Fi signal.
  The replay sends none of those.
