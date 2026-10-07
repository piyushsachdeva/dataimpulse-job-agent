"""Best-effort country from free-text job locations ("Toronto, ON", "Remote - UK", "NY")."""

import re

ISO3 = {"USA": "United States", "CAN": "Canada", "GBR": "United Kingdom", "IND": "India", "DEU": "Germany",
        "IRL": "Ireland", "NLD": "Netherlands", "FRA": "France", "ESP": "Spain", "SGP": "Singapore",
        "AUS": "Australia", "JPN": "Japan", "BRA": "Brazil", "MEX": "Mexico", "POL": "Poland", "ITA": "Italy",
        "ISR": "Israel", "CHN": "China", "KOR": "South Korea", "ARE": "UAE", "ZAF": "South Africa",
        "SWE": "Sweden", "CHE": "Switzerland", "LUX": "Luxembourg", "CRI": "Costa Rica", "ROU": "Romania",
        "CZE": "Czechia", "PHL": "Philippines", "VNM": "Vietnam", "TWN": "Taiwan", "HKG": "Hong Kong",
        "NZL": "New Zealand", "ARG": "Argentina", "COL": "Colombia", "CHL": "Chile", "PRT": "Portugal",
        "SAU": "Saudi Arabia", "EGY": "Egypt", "TUR": "Turkey", "AUT": "Austria", "BEL": "Belgium",
        "DNK": "Denmark", "FIN": "Finland", "NOR": "Norway", "IDN": "Indonesia", "MYS": "Malaysia",
        "THA": "Thailand", "GRC": "Greece", "HUN": "Hungary", "SVK": "Slovakia", "SRB": "Serbia"}

PATTERNS = [  # order matters: first match wins
    ("Canada", r"canada|toronto|vancouver|montr[eé]al|ottawa|calgary|waterloo|\b(ON|BC|QC|AB|NS)\b"),
    ("United Kingdom", r"united kingdom|\buk\b|london|england|scotland|manchester|edinburgh|cardiff|britain"),
    ("Ireland", r"ireland|dublin"),
    ("Germany", r"germany|berlin|munich|m[üu]nchen|hamburg|frankfurt|d[üu]sseldorf|cologne"),
    ("Netherlands", r"netherlands|amsterdam"),
    ("France", r"france|paris"),
    ("Spain", r"spain|madrid|barcelona"),
    ("Italy", r"italy|milan|rome"),
    ("Poland", r"poland|warsaw|krak[oó]w"),
    ("Portugal", r"portugal|lisbon|aveiro|porto"),
    ("Sweden", r"sweden|stockholm"),
    ("India", r"india|bangalore|bengaluru|gurugram|gurgaon|hyderabad|pune|mumbai|chennai|delhi|noida"),
    ("Singapore", r"singapore|\bSG\b"),
    ("Australia", r"australia|sydney|melbourne"),
    ("Japan", r"japan|tokyo"),
    ("Brazil", r"brazil|s[aã]o paulo"),
    ("Mexico", r"mexico"),
    ("Israel", r"israel|tel aviv"),
    ("United States", r"united states|\busa?\b|remote us|new york|\bnyc?\b|san francisco|seattle|austin|boston|"
                      r"chicago|denver|atlanta|los angeles|california|texas|washington|\bd\.?c\b|"
                      r"\b(CA|TX|WA|NY|MA|IL|CO|GA|PA|AZ|FL|NJ|VA|NC|OR|UT|MN|OH|MI|MD|TN)\b|bay area|north america"),
]
COMPILED = [(c, re.compile(p, re.I)) for c, p in PATTERNS]
UPPER_ONLY = re.compile(r"\b(ON|BC|QC|AB|NS|SG|CA|TX|WA|NY|MA|IL|CO|GA|PA|AZ|FL|NJ|VA|NC|OR|UT|MN|OH|MI|MD|TN)\b")


def country_of(location: str, iso3: str | None = None) -> str:
    if iso3 and iso3.upper() in ISO3:
        return ISO3[iso3.upper()]
    loc = location or ""
    for country, rx in COMPILED:
        for m in rx.finditer(loc):
            token = m.group(0)
            # 2-letter codes count only when written in capitals ("CA" = California, "ca" = noise)
            if len(token) == 2 and UPPER_ONLY.fullmatch(token) and token != token.upper():
                continue
            return country
    if re.search(r"remote|global|anywhere|distributed|hybrid", loc, re.I):
        return "Remote / multi-country"
    return "Other"
