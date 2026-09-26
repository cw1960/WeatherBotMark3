"""
Verified catalog of every Polymarket "Highest temperature in <city>" market.

Source of truth for station mapping. Each entry was checked on 2026-09-26 against:
  * the market's own resolution rules (Gamma API `description`), and
  * the aviationweather.gov station registry (ICAO code + coordinates).

`rules_phrase` must appear in the market rules text (after normalisation — see
markets.normalise). If Polymarket ever changes a city's resolution station, the
phrase check fails and the bot refuses to trade that city until this file is updated.

Units: US cities resolve in °F (2-degree buckets); everywhere else in °C (1-degree buckets).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogEntry:
    city_slug: str            # as used in the Polymarket event slug
    city_label: str
    icao: str
    airport: str
    rules_phrase: str | None  # None = not yet verified from rules → never traded
    unit: str                 # 'F' | 'C'
    timezone: str
    lat: float
    lon: float
    elev_m: int
    country: str
    resolution_source: str = "noaa"   # 'noaa' | 'wunderground' | 'hko'
    nws_office: str | None = None     # US only: WFO for Area Forecast Discussions
    active: bool = True


CATALOG: tuple[CatalogEntry, ...] = (
    # ---------------------------------------------------------------- United States (°F)
    CatalogEntry("nyc", "New York City", "KLGA", "LaGuardia Airport", "LaGuardia", "F", "America/New_York", 40.77945, -73.88027, 9, "US", nws_office="OKX"),
    CatalogEntry("atlanta", "Atlanta", "KATL", "Hartsfield-Jackson Intl", "Hartsfield-Jackson", "F", "America/New_York", 33.62972, -84.44223, 309, "US", nws_office="FFC"),
    CatalogEntry("miami", "Miami", "KMIA", "Miami Intl", "Miami Intl Airport", "F", "America/New_York", 25.78806, -80.31692, 1, "US", nws_office="MFL"),
    CatalogEntry("chicago", "Chicago", "KORD", "O'Hare Intl", "O'Hare", "F", "America/Chicago", 41.96017, -87.93161, 202, "US", nws_office="LOT"),
    CatalogEntry("dallas", "Dallas", "KDAL", "Dallas Love Field", "Love Field", "F", "America/Chicago", 32.83836, -96.83584, 148, "US", nws_office="FWD"),
    CatalogEntry("austin", "Austin", "KAUS", "Austin-Bergstrom Intl", "Austin-Bergstrom", "F", "America/Chicago", 30.1831, -97.68063, 148, "US", nws_office="EWX"),
    CatalogEntry("houston", "Houston", "KHOU", "William P. Hobby Airport", "Hobby", "F", "America/Chicago", 29.64582, -95.28214, 13, "US", nws_office="HGX"),
    CatalogEntry("denver", "Denver", "KBKF", "Buckley Space Force Base", "Buckley", "F", "America/Denver", 39.713, -104.758, 1703, "US", nws_office="BOU"),
    CatalogEntry("los-angeles", "Los Angeles", "KLAX", "Los Angeles Intl", "Los Angeles International", "F", "America/Los_Angeles", 33.93817, -118.3866, 30, "US", nws_office="LOX"),
    CatalogEntry("san-francisco", "San Francisco", "KSFO", "San Francisco Intl", "San Francisco International", "F", "America/Los_Angeles", 37.61961, -122.36561, 2, "US", nws_office="MTR"),
    CatalogEntry("seattle", "Seattle", "KSEA", "Seattle-Tacoma Intl", "Seattle-Tacoma", "F", "America/Los_Angeles", 47.44467, -122.31442, 115, "US", nws_office="SEW"),

    # ---------------------------------------------------------------- Americas (°C)
    CatalogEntry("toronto", "Toronto", "CYYZ", "Toronto Pearson Intl", "Pearson", "C", "America/Toronto", 43.679, -79.629, 171, "CA"),
    CatalogEntry("mexico-city", "Mexico City", "MMMX", "Benito Juárez Intl", "Benito Juarez", "C", "America/Mexico_City", 19.436, -99.072, 2224, "MX"),
    CatalogEntry("panama-city", "Panama City", "MPMG", "Marcos A. Gelabert Intl", "Gelabert", "C", "America/Panama", 8.967, -79.555, 6, "PA"),
    CatalogEntry("sao-paulo", "São Paulo", "SBGR", "São Paulo-Guarulhos Intl", "Guarulhos", "C", "America/Sao_Paulo", -23.432, -46.469, 745, "BR"),
    CatalogEntry("buenos-aires", "Buenos Aires", "SAEZ", "Ministro Pistarini Intl (Ezeiza)", "Pistarini", "C", "America/Argentina/Buenos_Aires", -34.822, -58.536, 16, "AR"),

    # ---------------------------------------------------------------- Europe (°C)
    CatalogEntry("london", "London", "EGLC", "London City Airport", "London City", "C", "Europe/London", 51.505, 0.055, 10, "GB"),
    CatalogEntry("paris", "Paris", "LFPB", "Paris-Le Bourget", "Le Bourget", "C", "Europe/Paris", 48.967, 2.428, 50, "FR"),
    CatalogEntry("amsterdam", "Amsterdam", "EHAM", "Amsterdam Schiphol", "Schiphol", "C", "Europe/Amsterdam", 52.315, 4.79, -2, "NL"),
    CatalogEntry("madrid", "Madrid", "LEMD", "Adolfo Suárez Madrid-Barajas", "Madrid-Barajas", "C", "Europe/Madrid", 40.466, -3.555, 589, "ES"),
    CatalogEntry("munich", "Munich", "EDDM", "Munich Airport", "Munich Airport", "C", "Europe/Berlin", 48.348, 11.813, 445, "DE"),
    CatalogEntry("milan", "Milan", "LIMC", "Milan Malpensa", "Malpensa", "C", "Europe/Rome", 45.631, 8.728, 221, "IT"),
    CatalogEntry("warsaw", "Warsaw", "EPWA", "Warsaw Chopin", "Chopin", "C", "Europe/Warsaw", 52.163, 20.961, 107, "PL"),
    CatalogEntry("helsinki", "Helsinki", "EFHK", "Helsinki-Vantaa", "Vantaa", "C", "Europe/Helsinki", 60.327, 24.957, 56, "FI"),
    CatalogEntry("moscow", "Moscow", "UUWW", "Vnukovo Intl", "Vnukovo", "C", "Europe/Moscow", 55.592, 37.261, 195, "RU"),
    CatalogEntry("istanbul", "Istanbul", "LTFM", "Istanbul Airport", "Istanbul Airport", "C", "Europe/Istanbul", 41.262, 28.74, 99, "TR"),
    CatalogEntry("ankara", "Ankara", "LTAC", "Esenboğa Intl", "Esenboga", "C", "Europe/Istanbul", 40.128, 32.995, 952, "TR"),

    # ---------------------------------------------------------------- Middle East / Africa / South Asia (°C)
    CatalogEntry("tel-aviv", "Tel Aviv", "LLBG", "Ben Gurion Intl", "Ben Gurion", "C", "Asia/Jerusalem", 32.011, 34.887, 35, "IL"),
    CatalogEntry("jeddah", "Jeddah", "OEJN", "King Abdulaziz Intl", "King Abdulaziz", "C", "Asia/Riyadh", 21.685, 39.166, 8, "SA"),
    CatalogEntry("cape-town", "Cape Town", "FACT", "Cape Town Intl", "Cape Town International", "C", "Africa/Johannesburg", -33.965, 18.602, 48, "ZA"),
    CatalogEntry("karachi", "Karachi", "OPKC", "Jinnah Intl", "Jinnah", "C", "Asia/Karachi", 24.902, 67.139, 20, "PK"),
    CatalogEntry("lucknow", "Lucknow", "VILK", "Chaudhary Charan Singh Intl", "Chaudhary Charan Singh", "C", "Asia/Kolkata", 26.761, 80.889, 121, "IN"),

    # ---------------------------------------------------------------- East / Southeast Asia & Oceania (°C)
    CatalogEntry("tokyo", "Tokyo", "RJTT", "Tokyo Haneda", "Haneda", "C", "Asia/Tokyo", 35.553, 139.781, 5, "JP"),
    CatalogEntry("seoul", "Seoul", "RKSI", "Incheon Intl", "Incheon", "C", "Asia/Seoul", 37.469, 126.451, 7, "KR"),
    CatalogEntry("busan", "Busan", "RKPK", "Gimhae Intl", "Gimhae", "C", "Asia/Seoul", 35.179, 128.938, 3, "KR"),
    CatalogEntry("beijing", "Beijing", "ZBAA", "Beijing Capital Intl", "Beijing Capital", "C", "Asia/Shanghai", 40.082, 116.603, 31, "CN"),
    CatalogEntry("shanghai", "Shanghai", "ZSPD", "Shanghai Pudong Intl", "Pudong", "C", "Asia/Shanghai", 31.146, 121.8, 4, "CN"),
    CatalogEntry("guangzhou", "Guangzhou", "ZGGG", "Guangzhou Baiyun Intl", "Baiyun", "C", "Asia/Shanghai", 23.392, 113.307, 11, "CN"),
    CatalogEntry("shenzhen", "Shenzhen", "ZGSZ", "Shenzhen Bao'an Intl", "Shenzhen Bao'an", "C", "Asia/Shanghai", 22.639, 113.803, 18, "CN"),
    CatalogEntry("wuhan", "Wuhan", "ZHHH", "Wuhan Tianhe Intl", "Tianhe", "C", "Asia/Shanghai", 30.783, 114.205, 33, "CN"),
    CatalogEntry("chongqing", "Chongqing", "ZUCK", "Chongqing Jiangbei Intl", "Jiangbei", "C", "Asia/Shanghai", 29.718, 106.639, 416, "CN"),
    CatalogEntry("chengdu", "Chengdu", "ZUUU", "Chengdu Shuangliu Intl", "Shuangliu", "C", "Asia/Shanghai", 30.576, 103.95, 494, "CN"),
    CatalogEntry("qingdao", "Qingdao", "ZSQD", "Qingdao Jiaodong Intl", "Jiaodong", "C", "Asia/Shanghai", 36.362, 120.087, 2, "CN"),
    CatalogEntry("taipei", "Taipei", "RCSS", "Taipei Songshan", "Songshan", "C", "Asia/Taipei", 25.069, 121.552, 8, "TW", resolution_source="wunderground"),
    CatalogEntry("manila", "Manila", "RPLL", "Ninoy Aquino Intl", "Ninoy Aquino", "C", "Asia/Manila", 14.507, 121.004, 15, "PH"),
    CatalogEntry("kuala-lumpur", "Kuala Lumpur", "WMKK", "Kuala Lumpur Intl", "Kuala Lumpur Intl", "C", "Asia/Kuala_Lumpur", 2.747, 101.714, 21, "MY"),
    CatalogEntry("singapore", "Singapore", "WSSS", "Singapore Changi", "Changi", "C", "Asia/Singapore", 1.368, 103.982, 17, "SG"),
    CatalogEntry("wellington", "Wellington", "NZWN", "Wellington Intl", "Wellington Intl", "C", "Pacific/Auckland", -41.331, 174.806, 12, "NZ"),

    # ---------------------------------------------------------------- Not tradeable yet (kept so discovery recognises them)
    # Hong Kong resolves on the Hong Kong Observatory's 0.1°C daily max — not an airport METAR.
    CatalogEntry("hong-kong", "Hong Kong", "VHHH", "Hong Kong Observatory (HQ, not the airport)", "Hong Kong Observatory", "C", "Asia/Hong_Kong", 22.302, 114.174, 32, "HK", resolution_source="hko", active=False),
    # Listed intermittently; resolution rules not yet reviewed → phrase unknown.
    CatalogEntry("jinan", "Jinan", "ZSJN", "Jinan Yaoqiang Intl", None, "C", "Asia/Shanghai", 36.856, 117.206, 22, "CN", active=False),
    CatalogEntry("zhengzhou", "Zhengzhou", "ZHCC", "Zhengzhou Xinzheng Intl", None, "C", "Asia/Shanghai", 34.52, 113.834, 149, "CN", active=False),
)

BY_SLUG: dict[str, CatalogEntry] = {c.city_slug: c for c in CATALOG}


def to_station(entry: CatalogEntry):
    """Catalog entry → Station model (stable fake id) for running without the database."""
    from uuid import NAMESPACE_URL, uuid5

    from .models import Station

    return Station(
        id=uuid5(NAMESPACE_URL, f"weatherbotmark3/{entry.city_slug}"),
        city_slug=entry.city_slug, city_label=entry.city_label, icao_code=entry.icao,
        airport_name=entry.airport, rules_phrase=entry.rules_phrase, unit=entry.unit,
        timezone=entry.timezone, latitude=entry.lat, longitude=entry.lon,
        elevation_m=entry.elev_m, country=entry.country,
        resolution_source=entry.resolution_source, nws_office=entry.nws_office,
        is_active=entry.active,
    )
