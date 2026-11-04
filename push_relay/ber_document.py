"""C.3 Koordinaten 10.6 parser, with the same table boundaries as the apps.

Stop bars/runways remain reference points, never aircraft stands. Unknown
document editions are rejected rather than scanning unrelated coordinate tables.
"""
import re


def matches(pattern, value):
    return list(re.finditer(pattern, value, re.I))


def normalize_stand(value):
    pattern = r'^(?:STAND(?:POSITION)?|PARKPOSITION|POSITION|POS|ACFT STAND)?\s*[:#]?\s*(?:([A-Z]{1,3})[ -]?(\d{1,3})([A-Z]?)|(\d{2,3})([A-Z]?))(?:\s*\((?:STOPP?|HALT).*\)|\s+STOPP?.*)?$'
    m = re.fullmatch(pattern, value.upper().replace('\xa0', ' ').strip())
    if not m:
        return None
    prefix, digits, suffix, numeric, numeric_suffix = m.groups()
    number = int(digits or numeric)
    return (prefix or 'GAT') + f'{number:02d}' + (suffix or numeric_suffix or '') if number > 0 else None


def coordinates(text):
    patterns = [
        r'''(?<![A-Z])([NSEW])\s*(\d{1,3})[\s°º]+(\d{1,2})[\s'′’]+(\d{1,2}(?:[.,]\d+)?)\s*["″”]?''',
        r'''(?<![\d.])(\d{1,3})[\s°º]+(\d{1,2})[\s'′’]+(\d{1,2}(?:[.,]\d+)?)\s*["″”]?\s*([NSEW])(?![A-Z])''',
        r'(?<![A-Z0-9])(?:([NS])\s*(\d{2})(\d{2})(\d{2}(?:[.,]\d+)?)|([EW])\s*(\d{3})(\d{2})(\d{2}(?:[.,]\d+)?)|(\d{2})(\d{2})(\d{2}(?:[.,]\d+)?)\s*([NS])|(\d{3})(\d{2})(\d{2}(?:[.,]\d+)?)\s*([EW]))(?![A-Z0-9])'
    ]
    out = []
    for index, pattern in enumerate(patterns):
        for m in matches(pattern, text):
            if index == 0: groups = (1, 2, 3, 4)
            elif index == 1: groups = (4, 1, 2, 3)
            elif m[1]: groups = (1, 2, 3, 4)
            elif m[5]: groups = (5, 6, 7, 8)
            elif m[9]: groups = (12, 9, 10, 11)
            else: groups = (16, 13, 14, 15)
            h = m[groups[0]].upper()
            d, minute, second = [float(m[g].replace(',', '.')) for g in groups[1:]]
            if minute >= 60 or second >= 60:
                continue
            value = (d + minute/60 + second/3600) * (-1 if h in ('S', 'W') else 1)
            lat = h in ('N', 'S')
            if (lat and 51 <= value <= 53) or (not lat and 12 <= value <= 15):
                out.append((m.start(), m.end(), lat, value))
    for m in matches(r'(?<![\d.])([+-]?\d{1,3}[.,]\d{4,})(?!\d)', text):
        if any(a < m.end() and m.start() < b for a, b, _, _ in out):
            continue
        value = float(m[1].replace(',', '.'))
        if 51 <= value <= 53 or 12 <= value <= 15:
            out.append((m.start(), m.end(), 51 <= value <= 53, value))
    unique = []
    for c in sorted(out, key=lambda c: c[0]):
        if not any(a < c[1] and c[0] < b for a, b, _, _ in unique):
            unique.append(c)
    return unique


def section(label):
    value = label.upper()
    other = ('HOLDING', 'ZWISCHENHALT', 'ROLLHALT', 'STOP BAR', 'STOPBAR', 'HALTEBALKEN', 'TAXIWAY CROSS', 'ROLLBAHNKREUZ', 'KREUZUNG', 'THRESHOLD', 'SCHWELLE', 'LANDEBAHN', 'HELIPORT', 'REFERENCE POINT')
    if any(word in value for word in other) or re.search(r'\b(06L|24R|06R|24L|RUNWAY|RWY)\b', value):
        return 'other'
    if any(word in value for word in ('AIRCRAFT STAND', 'ACFT STAND', 'ABSTELLPOSITION', 'PARKPOSITION', 'STANDPOSITION')):
        return 'stands'
    return 'unknown'


def parse_page(text):
    result, pending, active, is_stand, lat, lon, age = [], None, 'unknown', False, None, None, 0
    for raw in text.splitlines():
        line = raw.replace('\xa0', ' ').replace('\u202f', ' ').strip()
        if not line:
            continue
        coords = coordinates(line)
        prefix = (line[:coords[0][0]] if coords else line).strip()
        prefix = re.sub(r'[|:;]+$', '', prefix).strip()
        stand, inferred = normalize_stand(prefix), section(prefix)
        if not coords and inferred != 'unknown' and stand is None:
            active = inferred
        if prefix and not re.fullmatch(r'(?:[NE]|(?:LAT|LON)(?:ITUDE|GITUDE)?)', prefix, re.I):
            if stand or inferred != 'unknown' or coords:
                pending, is_stand = stand, bool(stand and (inferred == 'stands' or (inferred == 'unknown' and active != 'other')))
                lat, lon, age = None, None, 0
            elif len(line) < 60 and not re.search(r'WGS.?84|LATITUDE|LONGITUDE|SEITE|PAGE|KOORDINAT|COORDINAT|AIP', line, re.I):
                pending, lat, lon, age = None, None, None, 0
        age += 1
        if age > 5:
            pending, lat, lon = None, None, None
        end = 0
        for start, stop, latitude, value in coords:
            between = line[end:start].strip()
            next_stand = normalize_stand(between)
            if end > 0 and between and not re.fullmatch(r'[/|,;NE ]+', between, re.I) and next_stand:
                pending, is_stand, lat, lon = next_stand, active != 'other', None, None
            if latitude: lat = value
            else: lon = value
            if lat is not None and lon is not None:
                if pending and is_stand:
                    result.append(dict(name=pending, latitude=lat, longitude=lon, kind='stand'))
                lat, lon, age = None, None, 0
            end = stop
    return result


def parse_document(pages):
    if len(pages) < 38:
        raise ValueError('Unrecognized BER document')
    first, last = pages[18].splitlines(), pages[37].splitlines()
    starts = [i for i, line in enumerate(first) if re.match(r'^\s*19\s*\(\s*Stopp\s*1\s*\)', line, re.I)]
    ends = [i for i, line in enumerate(last) if re.match(r'^\s*E\s*35\s*\(\s*Stopp\s*2\s*\)', line, re.I)]
    if not starts or not ends:
        raise ValueError('Unrecognized BER table boundaries')
    start, end = starts[0], ends[-1]
    tail = last[:end+1]
    for line in last[end+1:end+5]:
        line = line.strip()
        if not line:
            continue
        coords = coordinates(line)
        prefix = (line[:coords[0][0]] if coords else line).strip().upper()
        if prefix and prefix not in ('N', 'E', 'LAT', 'LON'):
            break
        tail.append(line)
    result = []
    for page in ['\n'.join(first[start:]), *pages[19:37], '\n'.join(tail)]:
        result.extend(parse_page(page))
    if not result:
        raise ValueError('No aircraft stands in BER table')
    for index in range(3, 38):
        lines = pages[index].splitlines()
        if index == 18: lines = first[:start]
        elif index == 37: lines = last[end+1:]
        elif index >= 19: continue
        kind = ('stop_bar_cat_i' if index == 5 else 'stop_bar_cat_ii_iii' if 6 <= index <= 7 else 'holding' if 8 <= index <= 14 else 'crossing' if 15 <= index <= 17 else 'reference')
        label, lat, lon, age = '', None, None, 0
        for line in lines:
            line = line.strip()
            if not line: continue
            coords = coordinates(line)
            prefix = (line[:coords[0][0]] if coords else line).strip()
            if prefix and prefix.upper() not in ('N', 'E', 'LAT', 'LON'):
                label, lat, lon, age = prefix, None, None, 0
            age += 1
            if age > 5: label, lat, lon = '', None, None
            for _, _, latitude, value in coords:
                if latitude: lat = value
                else: lon = value
                if lat is not None and lon is not None and label:
                    actual = 'runway' if index == 3 and re.match(r'^(06L|24R|06R|24L)\b', label, re.I) else kind
                    result.append(dict(name=label, latitude=lat, longitude=lon, kind=actual))
                    lat, lon = None, None
    unique = {}
    for row in result:
        unique.setdefault((row['kind'], row['name'], round(row['latitude'], 7), round(row['longitude'], 7)), row)
    return list(unique.values())
