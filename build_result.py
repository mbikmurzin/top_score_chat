from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font, PatternFill


SOURCE = Path(r"C:\Users\User\Downloads\Копия (С1) Выгрузка подписчиков 14.09.xlsx")
OUTPUT = Path(__file__).with_name("Подписчики_первые_по_учебному_году_с_tamtam_user_id.xlsx")
EXPECTED_HEADERS = (
    "ВОРОНКА", "client_id", "utm_source", "utm_medium", "utm_campaign",
    "utm_content", "utm_term", "tag", "current_date", "current_time",
    "tamtam_user_id",
)


def academic_year(value):
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        raise ValueError(f"current_date must be a date, got {type(value).__name__}")
    if date(2026, 3, 1) <= value < date(2026, 5, 1):
        return 26
    if date(2026, 5, 1) <= value < date(2027, 5, 1):
        return 27
    return None


def timestamp(day_value, time_value):
    if isinstance(day_value, datetime):
        day_value = day_value.date()
    if isinstance(time_value, datetime):
        time_value = time_value.time()
    elif isinstance(time_value, (int, float)):
        time_value = (datetime.min + timedelta(days=time_value)).time()
    if not isinstance(time_value, time):
        raise ValueError(f"current_time must be a time, got {type(time_value).__name__}")
    return datetime.combine(day_value, time_value)


def client_key(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


source_book = load_workbook(SOURCE, read_only=True, data_only=True)
source_sheet = source_book["Лист2"]
rows = source_sheet.iter_rows(min_row=1, max_col=11, values_only=False)
headers = tuple(cell.value for cell in next(rows))
if headers != EXPECTED_HEADERS:
    raise ValueError(f"Unexpected A:J headers: {headers!r}")

winners = {}
exact_ids = defaultdict(set)
rounded_ids = defaultdict(set)
input_rows = 0
excluded = Counter()
client_types = Counter()

for row_number, cells in enumerate(rows, start=2):
    row = tuple(cell.value for cell in cells)
    input_rows += 1
    client_id = row[1]
    if client_id is None or client_id == "":
        excluded["blank_client_id"] += 1
        continue
    client_types[type(client_id).__name__] += 1
    tamtam_id = row[10]
    if tamtam_id is not None:
        if cells[10].number_format == "0.00E+00":
            rounded_ids[client_key(client_id)].add(tamtam_id)
        elif isinstance(tamtam_id, int):
            exact_ids[client_key(client_id)].add(tamtam_id)
        else:
            raise ValueError(f"Unexpected tamtam_user_id type at K{row_number}: {type(tamtam_id).__name__}")
    year = academic_year(row[8])
    if year is None:
        excluded["outside_period"] += 1
        continue
    moment = timestamp(row[8], row[9])
    key = (client_key(client_id), year)
    previous = winners.get(key)
    if previous is None or moment < previous[0]:
        winners[key] = (moment, row_number, row[:10])

selected = sorted(winners.values(), key=lambda item: item[1])
year_counts = Counter(key[1] for key in winners)
conflicts = {client: values for client, values in exact_ids.items() if len(values) > 1}
if conflicts:
    raise ValueError(f"Conflicting exact tamtam_user_id values for {len(conflicts)} clients")
resolved = {client: next(iter(values)) for client, values in exact_ids.items()}
expected_rows = []
id_counts = Counter()
for _, _, row in selected:
    client = client_key(row[1])
    exact_id = resolved.get(client)
    expected_rows.append((*row, exact_id))
    if exact_id is not None:
        id_counts["exact"] += 1
    elif rounded_ids.get(client):
        id_counts["rounded_only"] += 1
    else:
        id_counts["missing_in_source"] += 1

output_book = Workbook(write_only=True)
output_sheet = output_book.create_sheet("Результат")
output_sheet.freeze_panes = "A2"
for col, width in {"A": 25, "B": 18, "C": 20, "D": 20, "E": 23, "F": 20, "G": 20, "H": 35, "I": 16, "J": 14, "K": 20}.items():
    output_sheet.column_dimensions[col].width = width

header_cells = []
for value in headers:
    cell = WriteOnlyCell(output_sheet, value=value)
    cell.font = Font(bold=True)
    cell.fill = PatternFill(fill_type="solid", fgColor="DCEAF7")
    header_cells.append(cell)
output_sheet.append(header_cells)

for row in expected_rows:
    cells = []
    for column, value in enumerate(row, start=1):
        cell = WriteOnlyCell(output_sheet, value=value)
        if isinstance(value, str):
            cell.data_type = "s"
        if column == 9:
            cell.number_format = "dd.mm.yyyy"
        elif column == 10:
            cell.number_format = "hh:mm"
        elif column == 11:
            cell.number_format = "0"
        cells.append(cell)
    output_sheet.append(cells)

output_sheet.auto_filter.ref = f"A1:K{len(selected) + 1}"
output_book.save(OUTPUT)
source_book.close()

check_book = load_workbook(OUTPUT, read_only=True, data_only=True)
check_sheet = check_book["Результат"]
if tuple(next(check_sheet.iter_rows(min_row=1, max_row=1, max_col=11, values_only=True))) != headers:
    raise AssertionError("Output headers differ from source")
check_rows = check_sheet.iter_rows(min_row=2, max_col=11, values_only=True)
for position, (actual, expected) in enumerate(zip(check_rows, expected_rows, strict=True), start=2):
    if tuple(actual) != tuple(expected):
        raise AssertionError(f"Output differs from selected source row at output row {position}")
check_book.close()

print(f"source_rows={input_rows}")
print(f"result_rows={len(selected)}")
print(f"year_26={year_counts[26]} year_27={year_counts[27]}")
print(f"tamtam_ids={dict(id_counts)}")
print(f"excluded={dict(excluded)} client_types={dict(client_types)}")
print(f"output={OUTPUT}")
