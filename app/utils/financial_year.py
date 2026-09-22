from datetime import date


def financial_year_for(on_date: date) -> str:
    """India's FY runs April 1 - March 31. Returns e.g. "2025-26"."""
    if on_date.month >= 4:
        start_year = on_date.year
    else:
        start_year = on_date.year - 1
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def current_financial_year() -> str:
    return financial_year_for(date.today())
