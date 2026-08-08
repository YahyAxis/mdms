"""
Geographic & Regional Data Utility
Provides unified country-code to geographical region mappings across enrichment and discovery engines.
"""

from typing import Dict, Optional

COUNTRY_REGION_MAP: Dict[str, str] = {
    # North America
    "US": "North America", "USA": "North America", "UNITED STATES": "North America",
    "CA": "North America", "CAN": "North America", "CANADA": "North America",
    "MX": "North America", "MEX": "North America", "MEXICO": "North America",

    # Europe
    "GB": "Europe", "UK": "Europe", "GBR": "Europe", "UNITED KINGDOM": "Europe",
    "DE": "Europe", "DEU": "Europe", "GERMANY": "Europe",
    "FR": "Europe", "FRA": "Europe", "FRANCE": "Europe",
    "IT": "Europe", "ITA": "Europe", "ITALY": "Europe",
    "ES": "Europe", "ESP": "Europe", "SPAIN": "Europe",
    "SE": "Europe", "SWE": "Europe", "SWEDEN": "Europe",
    "NO": "Europe", "NOR": "Europe", "NORWAY": "Europe",
    "FI": "Europe", "FIN": "Europe", "FINLAND": "Europe",
    "DK": "Europe", "DNK": "Europe", "DENMARK": "Europe",
    "NL": "Europe", "NLD": "Europe", "NETHERLANDS": "Europe",
    "BE": "Europe", "BEL": "Europe", "BELGIUM": "Europe",
    "AT": "Europe", "AUT": "Europe", "AUSTRIA": "Europe",
    "CH": "Europe", "CHE": "Europe", "SWITZERLAND": "Europe",
    "PL": "Europe", "POL": "Europe", "POLAND": "Europe",
    "IE": "Europe", "IRL": "Europe", "IRELAND": "Europe",
    "PT": "Europe", "PRT": "Europe", "PORTUGAL": "Europe",
    "GR": "Europe", "GRC": "Europe", "GREECE": "Europe",
    "RU": "Europe", "RUS": "Europe", "RUSSIA": "Europe",
    "UA": "Europe", "UKR": "Europe", "UKRAINE": "Europe",
    "CZ": "Europe", "CZE": "Europe", "CZECHIA": "Europe",
    "HU": "Europe", "HUN": "Europe", "HUNGARY": "Europe",
    "RO": "Europe", "ROU": "Europe", "ROMANIA": "Europe",
    "IS": "Europe", "ISL": "Europe", "ICELAND": "Europe",

    # Latin America
    "BR": "Latin America", "BRA": "Latin America", "BRAZIL": "Latin America",
    "AR": "Latin America", "ARG": "Latin America", "ARGENTINA": "Latin America",
    "CL": "Latin America", "CHL": "Latin America", "CHILE": "Latin America",
    "CO": "Latin America", "COL": "Latin America", "COLOMBIA": "Latin America",
    "PE": "Latin America", "PER": "Latin America", "PERU": "Latin America",
    "VE": "Latin America", "VEN": "Latin America", "VENEZUELA": "Latin America",
    "CU": "Latin America", "CUB": "Latin America", "CUBA": "Latin America",
    "PR": "Latin America", "PRI": "Latin America", "PUERTO RICO": "Latin America",
    "JM": "Latin America", "JAM": "Latin America", "JAMAICA": "Latin America",

    # East Asia
    "JP": "East Asia", "JPN": "East Asia", "JAPAN": "East Asia",
    "KR": "East Asia", "KOR": "East Asia", "SOUTH KOREA": "East Asia",
    "CN": "East Asia", "CHN": "East Asia", "CHINA": "East Asia",
    "TW": "East Asia", "TWN": "East Asia", "TAIWAN": "East Asia",
    "HK": "East Asia", "HKG": "East Asia", "HONG KONG": "East Asia",

    # Southeast Asia
    "ID": "Southeast Asia", "IDN": "Southeast Asia", "INDONESIA": "Southeast Asia",
    "TH": "Southeast Asia", "THA": "Southeast Asia", "THAILAND": "Southeast Asia",
    "PH": "Southeast Asia", "PHL": "Southeast Asia", "PHILIPPINES": "Southeast Asia",
    "MY": "Southeast Asia", "MYS": "Southeast Asia", "MALAYSIA": "Southeast Asia",
    "VN": "Southeast Asia", "VNM": "Southeast Asia", "VIETNAM": "Southeast Asia",
    "SG": "Southeast Asia", "SGP": "Southeast Asia", "SINGAPORE": "Southeast Asia",

    # South Asia
    "IN": "South Asia", "IND": "South Asia", "INDIA": "South Asia",
    "PK": "South Asia", "PAK": "South Asia", "PAKISTAN": "South Asia",
    "BD": "South Asia", "BGD": "South Asia", "BANGLADESH": "South Asia",

    # Middle East & North Africa
    "MA": "Middle East & North Africa", "MAR": "Middle East & North Africa", "MOROCCO": "Middle East & North Africa",
    "EG": "Middle East & North Africa", "EGY": "Middle East & North Africa", "EGYPT": "Middle East & North Africa",
    "DZ": "Middle East & North Africa", "DZA": "Middle East & North Africa", "ALGERIA": "Middle East & North Africa",
    "TN": "Middle East & North Africa", "TUN": "Middle East & North Africa", "TUNISIA": "Middle East & North Africa",
    "TR": "Middle East & North Africa", "TUR": "Middle East & North Africa", "TURKEY": "Middle East & North Africa",
    "IL": "Middle East & North Africa", "ISR": "Middle East & North Africa", "ISRAEL": "Middle East & North Africa",
    "AE": "Middle East & North Africa", "ARE": "Middle East & North Africa", "UNITED ARAB EMIRATES": "Middle East & North Africa",
    "SA": "Middle East & North Africa", "SAU": "Middle East & North Africa", "SAUDI ARABIA": "Middle East & North Africa",
    "IR": "Middle East & North Africa", "IRN": "Middle East & North Africa", "IRAN": "Middle East & North Africa",

    # Sub-Saharan Africa
    "ZA": "Sub-Saharan Africa", "ZAF": "Sub-Saharan Africa", "SOUTH AFRICA": "Sub-Saharan Africa",
    "NG": "Sub-Saharan Africa", "NGA": "Sub-Saharan Africa", "NIGERIA": "Sub-Saharan Africa",
    "KE": "Sub-Saharan Africa", "KEN": "Sub-Saharan Africa", "KENYA": "Sub-Saharan Africa",
    "GH": "Sub-Saharan Africa", "GHA": "Sub-Saharan Africa", "GHANA": "Sub-Saharan Africa",
    "ET": "Sub-Saharan Africa", "ETH": "Sub-Saharan Africa", "ETHIOPIA": "Sub-Saharan Africa",
    "SN": "Sub-Saharan Africa", "SEN": "Sub-Saharan Africa", "SENEGAL": "Sub-Saharan Africa",

    # Oceania
    "AU": "Oceania", "AUS": "Oceania", "AUSTRALIA": "Oceania",
    "NZ": "Oceania", "NZL": "Oceania", "NEW ZEALAND": "Oceania"
}

def get_region_for_country(country_code_or_name: Optional[str]) -> str:
    """
    Returns the mapped region for a country code or country name.
    Defaults to 'Global' if unmapped or unspecified.
    """
    if not country_code_or_name:
        return "Global"
    
    clean_key = str(country_code_or_name).strip().upper()
    return COUNTRY_REGION_MAP.get(clean_key, "Global")