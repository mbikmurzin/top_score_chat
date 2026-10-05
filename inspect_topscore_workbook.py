from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"m": MAIN, "r": DOC_REL}


def shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(node.text or "" for node in item.iter(f"{{{MAIN}}}t")) for item in root]


def sheet_path(archive: zipfile.ZipFile, wanted: str) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall(f"{{{REL}}}Relationship")}
    for sheet in workbook.findall(".//m:sheet", NS):
        if sheet.attrib["name"].casefold() == wanted.casefold():
            target = targets[sheet.attrib[f"{{{DOC_REL}}}id"]].lstrip("/")
            return target if target.startswith("xl/") else f"xl/{target}"
    raise KeyError(wanted)


def read_sheet(path: Path, sheet_name: str) -> dict[int, dict[str, dict[str, str | None]]]:
    with zipfile.ZipFile(path) as archive:
        strings = shared_strings(archive)
        target = sheet_path(archive, sheet_name)
        root = ET.fromstring(archive.read(target))
    rows: dict[int, dict[str, dict[str, str | None]]] = {}
    for cell in root.findall(".//m:c", NS):
        ref = cell.attrib["r"]
        row_number = int(re.search(r"\d+", ref).group())
        value_node = cell.find("m:v", NS)
        formula_node = cell.find("m:f", NS)
        value = value_node.text if value_node is not None else None
        if cell.attrib.get("t") == "s" and value is not None:
            value = strings[int(value)]
        elif cell.attrib.get("t") == "inlineStr":
            value = "".join(node.text or "" for node in cell.findall(".//m:t", NS))
        rows.setdefault(row_number, {})[ref] = {
            "value": value,
            "formula": formula_node.text if formula_node is not None else None,
            "style": cell.attrib.get("s"),
            "type": cell.attrib.get("t"),
        }
    return rows


def read_sheet_range(path: Path, sheet_name: str, from_row: int, to_row: int) -> dict[int, dict[str, dict[str, str | None]]]:
    rows: dict[int, dict[str, dict[str, str | None]]] = {}
    with zipfile.ZipFile(path) as archive:
        strings = shared_strings(archive)
        target = sheet_path(archive, sheet_name)
        for _, row in ET.iterparse(archive.open(target), events=("end",)):
            if row.tag != f"{{{MAIN}}}row":
                continue
            row_number = int(row.attrib["r"])
            if row_number > to_row:
                break
            if row_number >= from_row:
                for cell in row.findall("m:c", NS):
                    ref = cell.attrib["r"]
                    value_node = cell.find("m:v", NS)
                    formula_node = cell.find("m:f", NS)
                    value = value_node.text if value_node is not None else None
                    if cell.attrib.get("t") == "s" and value is not None:
                        value = strings[int(value)]
                    elif cell.attrib.get("t") == "inlineStr":
                        value = "".join(node.text or "" for node in cell.findall(".//m:t", NS))
                    rows.setdefault(row_number, {})[ref] = {
                        "value": value,
                        "formula": formula_node.text if formula_node is not None else None,
                        "style": cell.attrib.get("s"),
                        "type": cell.attrib.get("t"),
                    }
            row.clear()
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--sheet", default="Top Score по воронкам")
    parser.add_argument("--from-row", type=int, default=1)
    parser.add_argument("--to-row", type=int, default=250)
    args = parser.parse_args()
    rows = read_sheet(args.workbook, args.sheet)
    for row_number in range(args.from_row, args.to_row + 1):
        if row_number in rows:
            meaningful = {ref: cell for ref, cell in rows[row_number].items() if cell["value"] is not None or cell["formula"] is not None}
            if meaningful:
                print(row_number, json.dumps(meaningful, ensure_ascii=False))


if __name__ == "__main__":
    main()
