"""
秀悦AI视频自动化平台 — 入口文件
用法：
  正常运行：python main.py --input data/input/各组ai表格资料对接.xlsx
  废片重跑：python main.py --retry data/output/台账/20250521_处理台账.xlsx
  仅解析Excel（调试）：python main.py --input xxx.xlsx --parse-only
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── 读取配置 ──────────────────────────────────────────────────────────────────
def load_config() -> dict:
    config_path = Path('config/workflow_config.json')
    if not config_path.exists():
        print("❌ 找不到 config/workflow_config.json，请检查配置文件")
        sys.exit(1)
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def check_env():
    """检查必要的环境变量"""
    missing = []
    if not os.getenv('COZE_TOKEN_1'):
        missing.append('COZE_TOKEN_1')
    if not os.getenv('COZE_TOKEN_2'):
        missing.append('COZE_TOKEN_2')
    if missing:
        print(f"❌ 缺少环境变量：{', '.join(missing)}")
        print("   请复制 .env.example 为 .env 并填入 Token")
        sys.exit(1)


# ── 正常运行模式 ───────────────────────────────────────────────────────────────
async def run_normal(excel_path: str, parse_only: bool = False):
    from src.scheduler import run_all
    from src.excel_parser import parse_excel, save_group_json

    config = load_config()
    token1 = os.getenv('COZE_TOKEN_1')
    token2 = os.getenv('COZE_TOKEN_2')

    today = datetime.now().strftime('%Y%m%d')
    ledger_path = f'data/output/台账/{today}_处理台账.xlsx'

    if parse_only:
        # 仅解析 Excel，输出 JSON 文件（调试用）
        print("\n🔍 仅解析模式（不调用工作流）\n")
        groups = parse_excel(excel_path, config)
        for group_name, data in groups.items():
            path = save_group_json(group_name, data, 'data/input/parsed')
            print(f"  💾 {group_name} → {path}")
        print("\n✅ 解析完成，JSON 文件已保存到 data/input/parsed/\n")
        return

    await run_all(excel_path, config, token1, token2, ledger_path)


# ── 废片重跑模式 ───────────────────────────────────────────────────────────────
async def run_retry(ledger_path: str):
    from src.ledger import read_retry_ids

    config = load_config()
    token2 = os.getenv('COZE_TOKEN_2')

    retry_ids = read_retry_ids(ledger_path)
    if not retry_ids:
        print("✅ 台账中没有标记废片（废片标记=Y）的条目，无需重跑")
        return

    print(f"\n🔁 废片重跑模式：共 {len(retry_ids)} 条需要重新生成\n")
    # TODO: 根据 retry_ids 从台账读取原始数据，重新调用工作流2，更新台账并下载
    print("⚠️  废片重跑功能开发中，敬请期待")


# ── 主入口 ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='秀悦AI视频自动化平台')
    parser.add_argument('--input',      type=str, help='甲方 Excel 文件路径')
    parser.add_argument('--retry',      type=str, help='台账 Excel 路径（废片重跑模式）')
    parser.add_argument('--parse-only', action='store_true', help='仅解析 Excel，不调用工作流')
    args = parser.parse_args()

    if not args.input and not args.retry:
        parser.print_help()
        sys.exit(1)

    check_env()

    if args.retry:
        asyncio.run(run_retry(args.retry))
    elif args.input:
        if not Path(args.input).exists():
            print(f"❌ 找不到 Excel 文件：{args.input}")
            sys.exit(1)
        asyncio.run(run_normal(args.input, parse_only=args.parse_only))


if __name__ == '__main__':
    main()
