#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Excel 处理模块
支持线程安全的读写操作
"""

import os
import threading
from typing import List, Dict, Any
from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet


class ExcelHandler:
    """Excel 处理器"""

    def __init__(self, file_path: str):
        self.file_path = os.path.abspath(file_path)
        self._lock = threading.Lock()
        self._workbook = None
        self._sheet = None
        self._load()

    def _load(self):
        if not os.path.exists(self.file_path):
            raise FileNotFoundError(f"Excel 文件不存在：{self.file_path}")
        self._workbook = load_workbook(self.file_path)
        self._sheet = self._workbook.active

    @property
    def sheet(self) -> Worksheet:
        return self._sheet

    def get_headers(self) -> List[str]:
        headers = []
        for cell in self._sheet[1]:
            headers.append(str(cell.value) if cell.value else "")
        return headers

    def get_test_cases(self) -> List[Dict[str, Any]]:
        """
        获取所有测试用例（过滤空白行）

        Returns:
            测试用例列表，每个用例包含字段：
            用例编号, 测试点, 操作步骤, 预期结果, _row
        """
        headers = self.get_headers()
        test_cases = []

        for row_idx, row in enumerate(
            self._sheet.iter_rows(min_row=2, values_only=True), start=2
        ):
            if not row or len(row) == 0 or row[0] is None or str(row[0]).strip() == '':
                continue

            case = {}
            for col_idx, value in enumerate(row):
                if col_idx < len(headers):
                    case[headers[col_idx]] = value if value else ""
            case['_row'] = row_idx
            test_cases.append(case)

        return test_cases

    def write_result(self, row: int, test_result: str, validation_reason: str):
        """写入测试结果和校验依据（线程安全）"""
        with self._lock:
            self._sheet[f'H{row}'] = test_result
            self._sheet[f'I{row}'] = validation_reason
            self._workbook.save(self.file_path)

    def close(self):
        if self._workbook:
            self._workbook.close()
            self._workbook = None
            self._sheet = None
