"""Read workbook values without executing formulas or importing a warehouse."""
from __future__ import annotations

import io
from pathlib import Path


def _open(raw, filename):
    if Path(filename).suffix.lower() == ".xlsx":
        import openpyxl
        return openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    import xlrd
    return xlrd.open_workbook(file_contents=raw, on_demand=True)


def workbook_sheets(raw: bytes, filename: str) -> list[str]:
    from .ingest import IngestError, upload_limits
    if Path(filename).suffix.lower() not in {".xlsx", ".xls"}:
        raise IngestError("请选择 .xlsx 或 .xls 文件")
    if not raw:
        raise IngestError("上传文件为空")
    if len(raw) > upload_limits()["max_bytes"]:
        raise IngestError("文件超过上传大小上限")
    book = None
    try:
        book = _open(raw, filename)
        names = list(book.sheetnames if hasattr(book, "sheetnames") else book.sheet_names())
        if not names:
            raise IngestError("Excel 文件没有可读取的工作表")
        return names
    except ImportError as exc:
        raise IngestError("Excel 解析组件未安装，请安装 openpyxl 和 xlrd") from exc
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError("Excel 无法读取，请确认文件未损坏、未加密，且扩展名与文件格式一致") from exc
    finally:
        if book is not None:
            book.close() if hasattr(book, "close") else book.release_resources()


def read_excel_frame(raw: bytes, filename: str, sheet_name: str = ""):
    import pandas as pd
    from .ingest import IngestError, upload_limits
    names = workbook_sheets(raw, filename)
    if not sheet_name and len(names) != 1:
        raise IngestError("Excel 包含多个工作表，请先选择本次分析使用的工作表")
    selected = sheet_name or names[0]
    if selected not in names:
        raise IngestError("所选工作表不存在，请重新选择")
    limit = upload_limits()["max_rows"]
    book = cached = None
    rows = []
    try:
        book = _open(raw, filename)
        if Path(filename).suffix.lower() == ".xlsx":
            import openpyxl
            cached = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
            sheet = book[selected]
            values = cached[selected].iter_rows()
            for formula_row, value_row in zip(sheet.iter_rows(), values):
                if len(rows) > limit:
                    raise IngestError(f"工作表超过 {limit} 行上限")
                row = []
                for formula, value in zip(formula_row, value_row):
                    if formula.data_type == "f" and value.value is None:
                        raise IngestError(f"工作表「{selected}」{formula.coordinate} 的公式没有已保存的计算值，请在 Excel 中计算并保存，或转为数值后上传")
                    if value.data_type == "e":
                        raise IngestError(f"工作表「{selected}」{value.coordinate} 含 Excel 错误值，请修正后上传")
                    row.append(value.value)
                rows.append(row)
        else:
            import xlrd
            sheet = book.sheet_by_name(selected)
            for idx in range(min(sheet.nrows, limit + 2)):
                row = []
                for cell in sheet.row(idx):
                    if cell.ctype == xlrd.XL_CELL_ERROR:
                        raise IngestError(f"工作表「{selected}」第 {idx + 1} 行含 Excel 错误值，请修正后上传")
                    value = cell.value
                    if cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}: value = None
                    elif cell.ctype == xlrd.XL_CELL_DATE: value = xlrd.xldate_as_datetime(value, book.datemode)
                    elif cell.ctype == xlrd.XL_CELL_BOOLEAN: value = bool(value)
                    row.append(value)
                rows.append(row)
        if not rows or not any(v is not None and str(v).strip() for v in rows[0]):
            raise IngestError("工作表首行为空，请将字段名放在第一行")
        if len(rows) > limit + 1:
            raise IngestError(f"工作表超过 {limit} 行上限")
        # Keep stored text (including leading zeroes), dates and numeric values.
        frame = pd.DataFrame(rows[1:], columns=[v if v is not None else "" for v in rows[0]], dtype=object)
        frame = frame.dropna(how="all")
        return frame, selected
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError("Excel 工作表解析失败，请检查文件内容") from exc
    finally:
        if cached is not None: cached.close()
        if book is not None: book.close() if hasattr(book, "close") else book.release_resources()
