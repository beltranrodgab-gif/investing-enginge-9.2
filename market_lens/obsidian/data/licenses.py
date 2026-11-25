"""Licensing register. Every series points at one entry here.

These are summaries written for an operator, not legal advice. Each entry links the governing
terms; read them before any use beyond personal research. ``commercial`` is deliberately
conservative: ``False`` means "do not assume it is allowed".
"""

LICENSES = {
    'fred': {
        'source': 'Federal Reserve Bank of St. Louis — FRED / ALFRED API',
        'terms_url': 'https://fred.stlouisfed.org/docs/api/terms_of_use.html',
        'allowed_use': 'Personal and research use with a free API key. Many series are '
                       'third-party copyrighted (e.g. ICE BofA indices, S&P, Moody\'s); their '
                       'notes restrict redistribution and history length.',
        'attribution': 'Source: FRED, Federal Reserve Bank of St. Louis',
        'requires_key': 'FRED_API_KEY',
        'commercial': False,
    },
    'ecb': {
        'source': 'European Central Bank — ECB Data Portal (SDMX REST)',
        'terms_url': 'https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html',
        'allowed_use': 'Reuse permitted, including commercial, provided the source is '
                       'acknowledged and the data are not misrepresented.',
        'attribution': 'Source: ECB statistics',
        'requires_key': None,
        'commercial': True,
    },
    'yahoo': {
        'source': 'Yahoo Finance chart endpoint (unofficial)',
        'terms_url': 'https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html',
        'allowed_use': 'Personal, non-commercial use only. Not an official or licensed API; '
                       'it can change or stop without notice. Replace with a licensed feed '
                       '(adapter interface) before any commercial or real-money use.',
        'attribution': 'Prices: Yahoo Finance',
        'requires_key': None,
        'commercial': False,
    },
    'marketlens-official': {
        'source': 'Official publishers collected by Market Lens (ONS, StatCan, Eurostat, BIS, '
                  'OECD, BLS, ECB, Bank of Canada, NY Fed, US Treasury)',
        'terms_url': 'see series_catalogue.csv and each publisher\'s terms',
        'allowed_use': 'Public official statistics; most permit reuse with attribution. '
                       'OECD and BIS terms differ by dataset — check before redistribution.',
        'attribution': 'Per publisher, as recorded in the source receipt',
        'requires_key': None,
        'commercial': False,
    },
}


def license_for(license_id):
    lic = LICENSES.get(license_id)
    if lic is None:
        raise KeyError('unknown license id: %s (every series must reference the register)' % license_id)
    return lic


def register():
    """The register as a list, for the CLI and the website."""
    return [dict(id=k, **v) for k, v in sorted(LICENSES.items())]
