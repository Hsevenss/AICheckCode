# -*- coding: utf-8 -*-
"""迷你验证器：不启 Flask 服务，直接跑一个小用例文件并对答案

用法：
    D:\\apython\\python.exe run_mini.py --dry      # 只解析用例，不调模型（免费）
    D:\\apython\\python.exe run_mini.py            # 真跑（约 0.01 元）
    D:\\apython\\python.exe run_mini.py -f bluelog_100.xlsx   # 换文件

跑完会逐条打印「AI 判定 vs 标准答案」，最后给准确率。
"""
import os
import sys
import time
import argparse

PROJ = r"D:\AITest\aitest\AICheckCode_sanitized\AICheckCode"
SRC = os.path.join(PROJ, "workspaces", "bluelog")
TC = os.path.join(PROJ, "test_cases")

sys.path.insert(0, PROJ)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-f", "--file", default="bluelog_3.xlsx", help="用例文件名")
    ap.add_argument("--dry", action="store_true", help="只解析，不调模型")
    args = ap.parse_args()

    path = os.path.join(TC, args.file)
    if not os.path.exists(path):
        print("找不到用例文件：%s" % path)
        print("可用：%s" % os.listdir(TC))
        sys.exit(1)

    # 用项目自己的解析器读，保证跟真实链路一致
    from checker.excel_handler import ExcelHandler
    cases = ExcelHandler(path).get_test_cases()

    print("=" * 72)
    print("用例文件: %s   解析到 %d 条" % (args.file, len(cases)))
    print("源码路径: %s" % SRC)
    print("=" * 72)

    if args.dry:
        for c in cases:
            print("  %-8s %-22s 标准答案=%s" % (
                c.get("用例编号"), str(c.get("测试点"))[:22], c.get("标准答案", "—")))
        print()
        print("（--dry 模式，未调用模型）")
        return

    from checker.verifier import FeatureVerifier
    v = FeatureVerifier()
    print("模式: %s" % v.analysis_mode)
    print()

    hit = 0
    for i, c in enumerate(cases, 1):
        cid = c.get("用例编号")
        point = c.get("测试点")
        expect = c.get("标准答案", "—")
        # 拼一段给模型的描述：测试点 + 操作步骤 + 预期结果
        desc = "【%s】%s\n操作步骤：%s\n预期结果：%s" % (
            cid, point, c.get("操作步骤", ""), c.get("预期结果", ""))

        print("[%d/%d] %s  %s" % (i, len(cases), cid, point))
        t = time.time()
        r = v.verify_feature(desc, SRC)
        got = r.get("功能是否完成", "?")
        ok = (got == expect)
        hit += 1 if ok else 0
        print("      耗时 %.1fs   AI判定=%s   标准答案=%s   %s" % (
            time.time() - t, got, expect, "命中" if ok else "漏判"))
        print("      理由: %s" % str(r.get("校验理由", ""))[:180])
        print()

    print("=" * 72)
    print("结果: %d/%d 命中" % (hit, len(cases)))
    print("=" * 72)


if __name__ == "__main__":
    main()
