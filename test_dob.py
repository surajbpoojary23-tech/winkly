import re
from datetime import date

def parse_dob(raw):
    raw = raw.strip()
    # YYYY-MM-DD (ISO) - check FIRST
    if re.match(r'^\d{4}-\d{2}-\d{2}$', raw):
        y, m, d = raw.split('-')
        return date(int(y), int(m), int(d))
    # DDMMYYYY (compact)
    if re.match(r'^\d{8}$', raw):
        return date(int(raw[4:8]), int(raw[2:4]), int(raw[0:2]))
    # DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY
    for sep in ('/', '-', '.'):
        if sep in raw:
            parts = raw.split(sep)
            if len(parts) == 3:
                d, m, y = parts
                if len(y) == 4 and y.isdigit():
                    return date(int(y), int(m), int(d))
                if len(y) == 2 and y.isdigit():
                    y = int(y) + (2000 if int(y) < 30 else 1900)
                    return date(y, int(m), int(d))
    # Month name: "15 Aug 1995", "15 August 1995", "Aug 15 1995"
    month_map = {
        'jan':1,'january':1,'feb':2,'february':2,'mar':3,'march':3,'apr':4,'april':4,
        'may':5,'jun':6,'june':6,'jul':7,'july':7,'aug':8,'august':8,'sep':9,'september':9,
        'oct':10,'october':10,'nov':11,'november':11,'dec':12,'december':12
    }
    parts = re.split(r'[\s,]+', raw)
    for i, part in enumerate(parts):
        if part.lower() in month_map:
            m = month_map[part.lower()]
            for d_part in parts:
                if d_part.isdigit() and 1 <= int(d_part) <= 31 and d_part != str(m):
                    for y_part in parts:
                        if y_part.isdigit() and len(y_part) in (2, 4) and y_part != d_part:
                            y = int(y_part)
                            if len(y_part) == 2:
                                y = y + (2000 if y < 30 else 1900)
                            return date(y, m, int(d_part))
    return None

test_cases = [
    ('15/08/1995', date(1995, 8, 15)),
    ('15-08-1995', date(1995, 8,15)),
    ('15.08.1995', date(1995, 8,15)),
    ('15/08/95', date(1995, 8,15)),
    ('1995-08-15', date(1995, 8,15)),
    ('15081995', date(1995, 8,15)),
    ('15 Aug 1995', date(1995, 8,15)),
    ('15 August 1995', date(1995, 8,15)),
    ('Aug 15 1995', date(1995, 8,15)),
    ('August 15, 1995', date(1995, 8,15)),
    ('invalid', None),
    ('15/08', None),
]

for inp, expected in test_cases:
    result = parse_dob(inp)
    status = 'PASS' if result == expected else 'FAIL'
    print(f'{status}: "{inp}" -> {result} (expected {expected})')